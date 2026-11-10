import { fromMarkdown } from "mdast-util-from-markdown";
import { gfmFromMarkdown } from "mdast-util-gfm";
import { gfm } from "micromark-extension-gfm";
import { decodeNamedCharacterReference } from "decode-named-character-reference";
import { decodeNumericCharacterReference } from "micromark-util-decode-numeric-character-reference";
import { parseFragment } from "parse5";
const rendered = new WeakMap(),
  cache = new Map();
const allowedHTML = new Set(
  "a abbr article aside b blockquote br caption code dd del details div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i kbd li main mark ol p pre s section small span strong sub summary sup table tbody td th thead tr u ul".split(
    " ",
  ),
);
const omittedHTML = new Set(
  "script style iframe object embed template noscript svg math head".split(" "),
);
const blockSelector = "p,h1,h2,h3,h4,h5,h6,pre,li,td,th,div,blockquote";
export function parse(source) {
  if (!cache.has(source)) {
    if (cache.size >= 8) cache.delete(cache.keys().next().value);
    cache.set(
      source,
      fromMarkdown(source, {
        extensions: [gfm()],
        mdastExtensions: [gfmFromMarkdown()],
      }),
    );
  }
  return cache.get(source);
}
export function isMarkdown(name, metadata = {}, source = "") {
  const named = [
    name,
    metadata.path,
    metadata.filename,
    metadata.source,
    metadata.url,
  ].some(
    (value) =>
      typeof value === "string" &&
      /\.(?:md|markdown|mdown)(?:$|[?#])/i.test(value),
  );
  if (named) return true;
  function formatted(node) {
    return (
      !["root", "paragraph", "text"].includes(node.type) ||
      node.children?.some(formatted)
    );
  }
  return Boolean(source && formatted(parse(source)));
}
// Each displayed UTF-16 unit carries a complete source span. Escapes, entities
// and surrogate pairs cannot be partially selected in the canonical source.
export function unitsFor(
  raw,
  offset = 0,
  decode = true,
  normalizeNewlines = false,
  decodeEscapes = decode,
) {
  const units = [];
  for (let index = 0; index < raw.length;) {
    const begin = index;
    let value;
    const entity =
      decode &&
      raw.slice(index).match(/^&(#(?:x[0-9a-f]+|[0-9]+)|[a-z][a-z0-9]+);/i);
    if (entity) {
      const name = entity[1];
      value =
        name[0] === "#"
          ? decodeNumericCharacterReference(
              name.slice(/^#x/i.test(name) ? 2 : 1),
              /^#x/i.test(name) ? 16 : 10,
            )
          : decodeNamedCharacterReference(name);
      if (value) index += entity[0].length;
    }
    if (
      !value &&
      decodeEscapes &&
      raw[index] === "\\" &&
      /[!-/:-@\[-`{-~]/.test(raw[index + 1] || "")
    ) {
      value = raw[index + 1];
      index += 2;
    }
    if (!value && normalizeNewlines && /[\r\n]/.test(raw[index])) {
      value = " ";
      index += raw[index] === "\r" && raw[index + 1] === "\n" ? 2 : 1;
    }
    if (!value) {
      value = String.fromCodePoint(raw.codePointAt(index));
      index += value.length;
    }
    for (let part = 0; part < value.length; part++)
      units.push({
        value: value[part],
        start: offset + begin,
        end: offset + index,
      });
  }
  return units;
}
function lines(value) {
  const result = [],
    regex = /([^\r\n]*)(\r\n|\r|\n|$)/g;
  for (let match; (match = regex.exec(value));) {
    result.push({ value: match[1], newline: match[2], offset: match.index });
    if (!match[2]) break;
  }
  return result;
}
function appendUnits(target, values) {
  for (const value of values) target.push(value);
}
function matchLines(raw, value, offset, decode) {
  const sourceLines = lines(raw),
    visibleLines = lines(value);
  if (sourceLines.length !== visibleLines.length) return null;
  const result = [];
  for (let index = 0; index < visibleLines.length; index++) {
    const source = sourceLines[index],
      visible = visibleLines[index],
      mapped = unitsFor(source.value, offset + source.offset, decode),
      decoded = mapped.map((u) => u.value).join("");
    if (!decoded.endsWith(visible.value)) return null;
    // mdast removes blockquote and list continuation prefixes from its value.
    appendUnits(result, mapped.slice(decoded.length - visible.value.length));
    if (visible.newline) {
      if (source.newline !== visible.newline) return null;
      appendUnits(
        result,
        unitsFor(
          source.newline,
          offset + source.offset + source.value.length,
          false,
        ),
      );
    }
  }
  return result;
}
export function leafUnits(node, source) {
  const start = node.position.start.offset,
    end = node.position.end.offset;
  let raw = source.slice(start, end),
    offset = start;
  if (node.type === "inlineCode") {
    const ticks = raw.match(/^`+/)?.[0].length || 0;
    raw = raw.slice(ticks, -ticks);
    offset += ticks;
    let units = unitsFor(raw, offset, false, true);
    if (
      units[0]?.value === " " &&
      units.at(-1)?.value === " " &&
      units.some((u) => u.value !== " ")
    )
      units = units.slice(1, -1);
    if (
      units.map((u) => u.value).join("") ===
      node.value.replace(/\r\n|\r|\n/g, " ")
    )
      return units;
    const mapped = matchLines(raw, node.value, offset, false);
    if (mapped)
      return mapped.map((u) => ({
        ...u,
        value: /[\r\n]/.test(u.value) ? " " : u.value,
      }));
    return unitsFor(raw, offset, false);
  }
  if (node.type === "code") {
    if (!node.value) return [];
    const sourceLines = lines(raw),
      fence = sourceLines[0].value.match(/^ {0,3}(`{3,}|~{3,})/);
    let content = sourceLines;
    if (fence) {
      content = content.slice(1);
      const closing = new RegExp(
        "^[ >\\t]*" + fence[1][0] + "{" + fence[1].length + ",}[ \\t]*$",
      );
      if (content.length && !content.at(-1).value && content.length > 1)
        content = content.slice(0, -1);
      if (content.length && closing.test(content.at(-1).value))
        content = content.slice(0, -1);
    }
    const visibleLines = lines(node.value),
      result = [];
    for (let index = 0; index < visibleLines.length; index++) {
      const visible = visibleLines[index],
        line = content[index];
      // Unusual container syntax stays literal rather than inventing offsets.
      if (!line || !line.value.endsWith(visible.value))
        return unitsFor(source.slice(start, end), start, false);
      appendUnits(
        result,
        unitsFor(
          visible.value,
          start + line.offset + line.value.length - visible.value.length,
          false,
        ),
      );
      if (visible.newline)
        appendUnits(
          result,
          unitsFor(
            visible.newline,
            start + line.offset + line.value.length,
            false,
          ),
        );
    }
    return result;
  }
  return (
    matchLines(raw, node.value, offset, true) || unitsFor(raw, offset, false)
  );
}
function lowerBound(units, predicate) {
  let lo = 0,
    hi = units.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (predicate(units[mid])) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}
export function safeURL(value) {
  try {
    const url = new URL(value, window.location.href);
    return ["https:", "http:", "mailto:"].includes(url.protocol)
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function render(
  root,
  source,
  annotations = [],
  decorate = null,
  formatted = true,
) {
  const state = { nodes: [], maps: new WeakMap() };
  root.replaceChildren();
  root.classList.toggle("markdown-rendered", formatted);
  rendered.set(root, state);
  function appendMapped(parent, units) {
    if (!units.length) return;
    const points = new Set([0, units.length]);
    for (const a of annotations) {
      points.add(lowerBound(units, (u) => u.end > a.start));
      points.add(lowerBound(units, (u) => u.start >= a.end));
    }
    const bounds = [...points].sort((a, b) => a - b);
    for (let index = 0; index < bounds.length - 1; index++) {
      const begin = bounds[index],
        end = bounds[index + 1];
      if (begin === end) continue;
      const mapped = units.slice(begin, end),
        active = annotations.filter(
          (a) => a.start < mapped.at(-1).end && a.end > mapped[0].start,
        ),
        text = document.createTextNode(mapped.map((u) => u.value).join(""));
      state.nodes.push(text);
      state.maps.set(text, mapped);
      if (active.length) {
        const mark = decorate
          ? decorate(text, active)
          : document.createElement("mark");
        if (!decorate) mark.append(text);
        parent.append(mark);
      } else parent.append(text);
    }
  }
  function link(element, href, title) {
    const url = safeURL(href);
    if (url) {
      element.href = url;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    if (title) element.title = title;
    element.addEventListener("click", (event) => {
      if (!event.ctrlKey && !event.metaKey) event.preventDefault();
    });
  }
  function html(parent, raw, base) {
    const fragment = parseFragment(raw, { sourceCodeLocationInfo: true });
    function visitHTML(node, target) {
      if (node.nodeName === "#text") {
        const loc = node.sourceCodeLocation;
        if (loc)
          appendMapped(
            target,
            unitsFor(
              raw.slice(loc.startOffset, loc.endOffset),
              base + loc.startOffset,
              true,
              false,
              false,
            ),
          );
        return;
      }
      const tag = node.tagName;
      if (omittedHTML.has(tag)) return;
      const element = allowedHTML.has(tag)
        ? document.createElement(tag)
        : target;
      if (element !== target) {
        if (tag === "a")
          link(
            element,
            node.attrs?.find((a) => a.name === "href")?.value || "",
            node.attrs?.find((a) => a.name === "title")?.value,
          );
        target.append(element);
      }
      for (const child of node.childNodes || []) visitHTML(child, element);
    }
    for (const node of fragment.childNodes) visitHTML(node, parent);
  }
  const definitions = new Map(),
    ast = formatted ? parse(source) : null;
  function collect(node) {
    if (node.type === "definition") definitions.set(node.identifier, node);
    node.children?.forEach(collect);
  }
  if (ast) collect(ast);
  function children(parent, nodes) {
    const stack = [{ element: parent, tag: null }];
    let suppressed = 0;
    for (const node of nodes || []) {
      if (node.type === "html") {
        const match = node.value.match(
          /^<\s*(\/?)\s*([a-z][a-z0-9]*)\b[^>]*>$/i,
        );
        if (match && !node.value.includes("\n")) {
          const closing = !!match[1],
            tag = match[2].toLowerCase();
          if (omittedHTML.has(tag)) {
            suppressed += closing ? -1 : 1;
            suppressed = Math.max(0, suppressed);
            continue;
          }
          if (suppressed) continue;
          if (closing) {
            const index = stack.findLastIndex((item) => item.tag === tag);
            if (index > 0) stack.length = index;
          } else if (allowedHTML.has(tag) && !["br", "hr"].includes(tag)) {
            const element = document.createElement(tag);
            if (tag === "a") {
              const parsed = parseFragment(node.value).childNodes[0];
              link(
                element,
                parsed?.attrs?.find((a) => a.name === "href")?.value || "",
              );
            }
            stack.at(-1).element.append(element);
            stack.push({ element, tag });
          } else
            html(stack.at(-1).element, node.value, node.position.start.offset);
          continue;
        }
      }
      if (!suppressed) visit(node, stack.at(-1).element);
    }
  }
  function visit(node, parent) {
    if (node.type === "table") {
      const table = document.createElement("table");
      parent.append(table);
      node.children.forEach((row, index) => {
        const section = document.createElement(index === 0 ? "thead" : "tbody");
        const tr = document.createElement("tr");
        section.append(tr);
        table.append(section);
        row.children.forEach((cell, column) => {
          const td = document.createElement(index === 0 ? "th" : "td");
          if (["left", "center", "right"].includes(node.align?.[column]))
            td.style.textAlign = node.align[column];
          tr.append(td);
          children(td, cell.children);
        });
      });
      return;
    }
    if (["definition", "footnoteDefinition"].includes(node.type)) return;
    if (["text", "inlineCode", "code"].includes(node.type)) {
      let target = parent;
      if (node.type === "inlineCode") {
        target = document.createElement("code");
        parent.append(target);
      }
      if (node.type === "code") {
        const pre = document.createElement("pre");
        target = document.createElement("code");
        pre.append(target);
        parent.append(pre);
      }
      appendMapped(target, leafUnits(node, source));
      return;
    }
    if (node.type === "html") {
      html(
        parent,
        source.slice(node.position.start.offset, node.position.end.offset),
        node.position.start.offset,
      );
      return;
    }
    if (node.type === "break") {
      parent.append(document.createElement("br"));
      return;
    }
    if (["image", "imageReference"].includes(node.type)) {
      // Image descriptions remain selectable; coding never needs remote image requests.
      const raw = source.slice(
          node.position.start.offset,
          node.position.end.offset,
        ),
        match = raw.match(/^!\[((?:\\.|[^\]])*)\]/);
      if (match) {
        const caption = document.createElement("span");
        caption.className = "markdown-image-description";
        appendMapped(
          caption,
          unitsFor(match[1], node.position.start.offset + 2, true),
        );
        parent.append(caption);
      }
      return;
    }
    const tag =
        {
          paragraph: "p",
          strong: "strong",
          emphasis: "em",
          delete: "del",
          blockquote: "blockquote",
          listItem: "li",
          link: "a",
          linkReference: "a",
          table: "table",
          tableRow: "tr",
          tableCell: "td",
          thematicBreak: "hr",
        }[node.type] ||
        (node.type === "heading"
          ? "h" + node.depth
          : node.type === "list"
            ? node.ordered
              ? "ol"
              : "ul"
            : null),
      target = tag ? document.createElement(tag) : parent;
    if (tag) parent.append(target);
    if (node.type === "list" && node.ordered && node.start !== 1)
      target.start = node.start;
    if (node.type === "listItem" && typeof node.checked === "boolean") {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = node.checked;
      checkbox.disabled = true;
      target.append(checkbox);
    }
    if (node.type === "link" || node.type === "linkReference") {
      const definition =
        node.type === "linkReference" ? definitions.get(node.identifier) : node;
      if (definition) link(target, definition.url, definition.title);
    }
    children(target, node.children);
  }
  if (formatted) visit(ast, root);
  else appendMapped(root, unitsFor(source, 0, false));
  return state;
}
export function selectionOffsets(root, range) {
  const state = rendered.get(root);
  if (
    !state ||
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  )
    return null;
  let start = null,
    end = null;
  for (const node of state.nodes) {
    if (!range.intersectsNode(node)) continue;
    const first = node === range.startContainer ? range.startOffset : 0,
      last = node === range.endContainer ? range.endOffset : node.length;
    if (last <= first) continue;
    const units = state.maps.get(node);
    if (start === null) start = units[first].start;
    if (end !== null && units[first].start < end) return null;
    end = units[last - 1].end;
  }
  return start !== null && end > start ? { start, end } : null;
}
export function restoreSelection(root, start, end, backward = false) {
  const state = rendered.get(root);
  if (!state) return;
  let first = null,
    last = null;
  for (const node of state.nodes) {
    const units = state.maps.get(node),
      begin = lowerBound(units, (u) => u.end > start),
      finish = lowerBound(units, (u) => u.start >= end);
    if (finish > begin) {
      if (!first) first = [node, begin];
      last = [node, finish];
    }
  }
  if (first && last)
    window
      .getSelection()
      .setBaseAndExtent(
        ...(backward ? [...last, ...first] : [...first, ...last]),
      );
}
export function visibleText(root, start, end) {
  const state = rendered.get(root);
  if (!state) return "";
  let result = "",
    block = null;
  for (const node of state.nodes) {
    const units = state.maps
      .get(node)
      .filter((u) => u.end > start && u.start < end);
    if (!units.length) continue;
    const current = node.parentElement.closest(blockSelector);
    if (result && block !== current) result += "\n";
    result += units.map((u) => u.value).join("");
    block = current;
  }
  return result;
}
