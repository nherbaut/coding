import cytoscape from "cytoscape";
import dagre from "cytoscape-dagre";
import edgehandles from "cytoscape-edgehandles";
import {
  clone,
  descendants,
  validate,
  connect,
  remove,
} from "./graph-model.js";
cytoscape.use(dagre);
cytoscape.use(edgehandles);
const $ = (s) => document.querySelector(s),
  cfg = $("#graph-config"),
  csrf = $("[name=csrfmiddlewaretoken]").value;
const readOnly = cfg.dataset.readonly === "true";
const el = (tag, text) => {
  const n = document.createElement(tag);
  if (text !== void 0) n.textContent = text;
  return n;
};
let book,
  saved,
  revision,
  referenced = /* @__PURE__ */ new Set(),
  selected = null,
  selectedEdge = null,
  undo = [],
  redo = [],
  collapsed = /* @__PURE__ */ new Set(),
  editorDirty = false,
  saving = false,
  drawing = false,
  references = [];
let cy,
  eh,
  colors = {};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const dirty = () => !readOnly && (editorDirty || !same(book, saved));
const feedback = (text, error = false) => {
  $("#graph-status").textContent = text;
  $("#graph-status").classList.toggle("error", error);
};
function controls() {
  $("#undo").disabled = !undo.length || saving;
  $("#redo").disabled = !redo.length || saving;
  $("#publish-book").disabled = !dirty() || saving;
  $("#discard").disabled = !dirty() || saving;
}
function change(next) {
  if (readOnly) return;
  validate(next);
  if (same(book, next)) return;
  undo.push(clone(book));
  redo = [];
  book = next;
  editorDirty = false;
  draw();
  inspect();
  feedback("Unsaved draft \xB7 Save codebook to publish.");
}
function guard() {
  return !editorDirty || confirm("Discard unapplied property edits?");
}
function select(id) {
  if (!guard()) return;
  selected = id;
  selectedEdge = null;
  editorDirty = false;
  $("#delete-edge").disabled = true;
  inspect();
  outline();
  cy.nodes().removeClass("chosen");
  cy.getElementById(id).addClass("chosen");
}
function ordered() {
  const rows = [];
  function visit(parent, depth) {
    for (const c of book.codes
      .filter((c2) => (c2.parent || null) === parent)
      .sort((a, b) => a.name.localeCompare(b.name))) {
      rows.push({ c, depth });
      visit(c.id, depth + 1);
    }
  }
  visit(null, 0);
  return rows;
}
function outline() {
  const root = $("#hierarchy-list");
  root.replaceChildren();
  const query = $("#graph-search").value.toLowerCase();
  for (const { c, depth } of ordered()) {
    if (query && !c.name.toLowerCase().includes(query)) continue;
    let parent = c.parent,
      hidden = false;
    while (parent) {
      if (collapsed.has(parent) && !query) hidden = true;
      parent = book.codes.find((n) => n.id === parent)?.parent;
    }
    if (hidden) continue;
    const row = el("div");
    row.className = "hierarchy-row";
    row.style.paddingLeft = `${depth * 12}px`;
    const toggle = el("button", collapsed.has(c.id) ? "\u25B8" : "\u25BE");
    toggle.className = "secondary hierarchy-toggle";
    toggle.setAttribute(
      "aria-label",
      `${collapsed.has(c.id) ? "Expand" : "Collapse"} ${c.name}`,
    );
    toggle.disabled = !book.codes.some((n) => n.parent === c.id);
    toggle.onclick = () => toggleCollapse(c.id);
    const button = el(
      "button",
      `${c.kind === "category" ? "\u25B0" : "\u25CF"} ${c.name}${c.archived ? " (archived)" : ""}`,
    );
    button.className = `secondary concept ${selected === c.id ? "selected" : ""} ${c.archived ? "archived" : ""}`;
    button.onclick = () => {
      select(c.id);
    };
    row.append(toggle, button);
    root.append(row);
  }
}
function toggleCollapse(id) {
  if (collapsed.has(id)) collapsed.delete(id);
  else collapsed.add(id);
  draw();
}
function draw(layout = true) {
  const pos = new Map(cy?.nodes().map((n) => [n.id(), n.position()]) || []);
  const elements = [];
  const hidden = /* @__PURE__ */ new Set();
  for (const id of collapsed)
    for (const child of descendants(book, id))
      if (child !== id) hidden.add(child);
  for (const c of book.codes) {
    elements.push({
      data: {
        id: c.id,
        label: c.name,
        color: c.color || colors[c.id] || "#bad5c8",
        kind: c.kind,
      },
      classes: `${c.archived ? "archived " : ""}${c.id === selected ? "chosen " : ""}${hidden.has(c.id) ? "collapsed " : ""}`,
      position: pos.get(c.id),
    });
    if (c.parent)
      elements.push({
        data: { id: `edge-${c.id}`, source: c.parent, target: c.id },
        classes: hidden.has(c.id) ? "collapsed" : "",
      });
  }
  cy.elements().remove();
  cy.add(elements);
  selectedEdge = null;
  $("#delete-edge").disabled = true;
  if (layout) autoLayout();
  outline();
  controls();
}
function fitGraph() {
  cy.fit(cy.elements(":visible"), 35);
  if (cy.zoom() > 1.25) {
    cy.zoom(1.25);
    cy.center(cy.elements(":visible"));
  }
}
function autoLayout() {
  cy.elements(":visible")
    .layout({
      name: "dagre",
      rankDir: "TB",
      nodeSep: 35,
      rankSep: 65,
      fit: false,
      padding: 35,
      animate: false,
    })
    .run();
  fitGraph();
}
function inspect() {
  const c = book.codes.find((c2) => c2.id === selected);
  $("#node-properties").hidden = !c;
  $("#no-concept").hidden = !!c;
  if (!c) return;
  for (const key of ["name", "kind", "memo", "rationale"])
    $(`#node-${key}`).value = c[key] || "";
  $("#node-color").value = c.color || colors[c.id] || "#bad5c8";
  $("#node-archived").checked = !!c.archived;
  $("#node-kind").disabled = readOnly || referenced.has(c.id);
  $("#node-parent").disabled = readOnly;
  $("#node-color").disabled = readOnly;
  $("#node-archived").disabled = readOnly;
  for (const field of ["name", "memo", "rationale"])
    $(`#node-${field}`).readOnly = readOnly;
  $("#node-status").textContent = referenced.has(c.id)
    ? "Used in coding. This identity and its type are protected."
    : "";
  const parent = $("#node-parent");
  parent.replaceChildren(new Option("Root concept", ""));
  const blocked = descendants(book, c.id);
  for (const { c: n, depth } of ordered())
    if (!blocked.has(n.id))
      parent.add(new Option(`${"\u2014 ".repeat(depth)}${n.name}`, n.id));
  parent.value = c.parent || "";
  references = clone(c.references || []);
  renderReferences();
  editorDirty = false;
  controls();
}
const referenceFields = [
  ["type", "Reference type"],
  ["citation_key", "Citation key"],
  ["citation", "Citation"],
  ["title", "Title"],
  ["authors", "Authors"],
  ["year", "Year"],
  ["location", "Exact source location"],
  ["url", "Source URL"],
  ["quote", "Verbatim excerpt"],
];
function renderReferences() {
  const root = $("#node-references");
  root.replaceChildren();
  references.forEach((ref, index) => {
    const card = el("details");
    card.className = "reference-card";
    card.open = true;
    card.append(
      el(
        "summary",
        ref.title ||
          ref.citation_key ||
          (ref.type === "corpus" ? "Corpus excerpt" : "Reference"),
      ),
    );
    if (ref.type === "corpus")
      card.append(
        el(
          "p",
          "Document version, hash and passage positions are retained and validated.",
        ),
      );
    for (const [key, label] of referenceFields) {
      if (ref[key] !== void 0 && typeof ref[key] === "object") continue;
      const wrap = el("label", label),
        input = el(key === "quote" ? "textarea" : "input");
      input.value = ref[key] ?? "";
      input.readOnly = readOnly;
      input.oninput = () => {
        if (input.value === "") delete ref[key];
        else
          ref[key] =
            key === "year" && /^\d+$/.test(input.value)
              ? Number(input.value)
              : input.value;
        editorDirty = true;
        controls();
      };
      wrap.append(input);
      card.append(wrap);
    }
    const del = el("button", "Remove reference");
    del.type = "button";
    del.hidden = readOnly;
    del.className = "secondary";
    del.onclick = () => {
      references.splice(index, 1);
      editorDirty = true;
      renderReferences();
      controls();
    };
    card.append(del);
    root.append(card);
  });
}
function apply() {
  if (readOnly) return false;
  if (!selected) return true;
  const next = clone(book),
    c = next.codes.find((c2) => c2.id === selected);
  for (const key of ["name", "kind", "memo", "rationale"])
    c[key] = $(`#node-${key}`).value;
  c.name = c.name.trim();
  c.parent = $("#node-parent").value || null;
  if (c.color || $("#node-color").value !== (colors[c.id] || "#bad5c8"))
    c.color = $("#node-color").value;
  c.archived = $("#node-archived").checked;
  c.references = clone(references);
  try {
    validate(next);
    editorDirty = false;
    change(next);
    controls();
    return true;
  } catch (e) {
    $("#node-status").textContent = e.message;
    return false;
  }
}
function deletionState() {
  const subtree = $("#delete-scope").value === "subtree",
    affected = subtree
      ? descendants(book, selected)
      : /* @__PURE__ */ new Set([selected]),
    protectedNodes = [...affected].some((id) => referenced.has(id));
  $("#delete-warning").textContent = protectedNodes
    ? "This selection contains concepts used in coding. Archive them to preserve their identities."
    : "This removal will be added to the local draft.";
  $("#confirm-delete").disabled = protectedNodes;
  $("#archive-instead").hidden = !protectedNodes;
}
async function request(url, body) {
  if (readOnly && body) throw Error("This codebook view is read-only.");
  const response = await fetch(
    url,
    body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(body),
        }
      : {},
  );
  const data = await response.json();
  if (!response.ok)
    throw Error(data.error || "Request failed. Check your study access.");
  return data;
}
function install(data) {
  colors = data.colors || colors;
  revision = data.revision;
  book = clone(data.codebook);
  saved = clone(book);
  referenced = new Set(data.referenced);
  undo = [];
  redo = [];
  editorDirty = false;
  draw();
  inspect();
  feedback(
    readOnly
      ? "Read-only personal codebook. Select a concept to view its details."
      : "Shared codebook saved.",
  );
}
async function start() {
  const data = await request(cfg.dataset.state);
  book = clone(data.codebook);
  saved = clone(book);
  revision = data.revision;
  referenced = new Set(data.referenced);
  cy = cytoscape({
    container: $("#codebook-graph"),
    elements: [],
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          "background-color": "data(color)",
          shape: "ellipse",
          width: 150,
          height: 52,
          "text-wrap": "wrap",
          "text-max-width": 140,
          "font-size": 12,
          "text-valign": "center",
          "border-color": "#436355",
          "border-width": 1,
        },
      },
      {
        selector: 'node[kind="category"]',
        style: { shape: "round-rectangle", "font-weight": "bold" },
      },
      {
        selector: "node.archived",
        style: { "border-style": "dashed", opacity: 0.55 },
      },
      {
        selector: "node.chosen",
        style: { "border-width": 4, "border-color": "#163e86" },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "line-color": "#8a9c94",
          "target-arrow-color": "#8a9c94",
        },
      },
      {
        selector: "edge:selected",
        style: {
          "line-color": "#c34232",
          "target-arrow-color": "#c34232",
          width: 4,
        },
      },
      { selector: ".collapsed", style: { display: "none" } },
      {
        selector: ".eh-handle",
        style: {
          "background-color": "#287b59",
          width: 12,
          height: 12,
          shape: "ellipse",
          "overlay-opacity": 0,
        },
      },
      {
        selector: ".eh-preview, .eh-ghost-edge",
        style: {
          "line-color": "#287b59",
          "target-arrow-color": "#287b59",
          "line-style": "dashed",
        },
      },
    ],
  });
  if (!readOnly)
    eh = cy.edgehandles({
      snap: true,
      noEdgeEventsInDraw: true,
      disableBrowserGestures: true,
      canConnect: (source, target) =>
        source.id() !== target.id() &&
        !descendants(book, target.id()).has(source.id()),
    });
  cy.on("ehcomplete", (_event, source, target, edge) => {
    edge.remove();
    try {
      change(connect(book, source.id(), target.id()));
    } catch (e) {
      feedback(e.message, true);
    }
  });
  cy.on("tap", "node", (event) => {
    if (!drawing) select(event.target.id());
  });
  cy.on("tap", "edge", (event) => {
    selectedEdge = event.target.id();
    $("#delete-edge").disabled = readOnly;
  });
  install(data);
}
$("#node-properties").oninput = () => {
  editorDirty = true;
  controls();
};
$("#node-properties").onsubmit = (e) => {
  e.preventDefault();
  apply();
};
$("#add-reference").onclick = () => {
  references.push({ type: "bibliographic" });
  editorDirty = true;
  renderReferences();
  controls();
};
$("#add-node").onclick = () => {
  if (!guard()) return;
  const next = clone(book);
  let name = "New concept",
    n = 2;
  while (next.codes.some((c) => c.parent === selected && c.name === name))
    name = `New concept ${n++}`;
  const id = crypto.randomUUID();
  next.codes.push({
    id,
    name,
    kind: "code",
    parent: selected,
    color: "#bad5c8",
    memo: "",
    rationale: "",
    references: [],
  });
  selected = id;
  change(next);
  $("#node-name").focus();
  $("#node-name").select();
};
$("#undo").onclick = () => {
  if (!guard() || !undo.length) return;
  redo.push(clone(book));
  book = undo.pop();
  editorDirty = false;
  draw();
  inspect();
  feedback("Draft undone.");
};
$("#redo").onclick = () => {
  if (!guard() || !redo.length) return;
  undo.push(clone(book));
  book = redo.pop();
  editorDirty = false;
  draw();
  inspect();
  feedback("Draft restored.");
};
$("#publish-book").onclick = async () => {
  if (editorDirty && !apply()) return;
  if (same(book, saved)) {
    feedback("No changes to publish.");
    return;
  }
  saving = true;
  controls();
  try {
    install(await request(cfg.dataset.publish, { revision, codebook: book }));
  } catch (e) {
    feedback(`${e.message} Your local draft is preserved.`, true);
  } finally {
    saving = false;
    controls();
  }
};
$("#discard").onclick = async () => {
  if (
    !confirm("Discard all local changes and load the current shared codebook?")
  )
    return;
  try {
    install(await request(cfg.dataset.state));
  } catch (e) {
    feedback(e.message, true);
  }
};
$("#fit").onclick = fitGraph;
$("#layout").onclick = autoLayout;
$("#expand-all").onclick = () => {
  collapsed.clear();
  draw();
};
$("#graph-search").oninput = outline;
$("#collapse-node").onclick = () => toggleCollapse(selected);
$("#draw-edge").onclick = () => {
  if (!guard()) return;
  drawing = !drawing;
  $("#draw-edge").setAttribute("aria-pressed", String(drawing));
  if (drawing) {
    eh.enableDrawMode();
    feedback(
      "Drag from a parent concept to its child. This replaces the child\u2019s existing parent.",
    );
  } else {
    eh.disableDrawMode();
    feedback(dirty() ? "Unsaved draft." : "Codebook ready.");
  }
};
$("#delete-edge").onclick = () => {
  if (!guard()) return;
  const edge = cy.getElementById(selectedEdge);
  if (edge.empty()) return;
  change(connect(book, null, edge.target().id()));
  selectedEdge = null;
  $("#delete-edge").disabled = true;
};
$("#delete-node").onclick = () => {
  if (!guard()) return;
  $("#delete-description").textContent =
    `Remove \u201C${book.codes.find((c) => c.id === selected).name}\u201D? Choose the scope each time.`;
  $("#delete-scope").value = "node";
  deletionState();
  $("#delete-dialog").showModal();
};
$("#delete-scope").onchange = deletionState;
function doDelete(archive) {
  try {
    const next = remove(
      book,
      selected,
      $("#delete-scope").value === "subtree",
      referenced,
      archive,
    );
    if (!archive) selected = null;
    change(next);
    $("#delete-dialog").close();
  } catch (e) {
    $("#delete-warning").textContent = e.message;
  }
}
$("#confirm-delete").onclick = () => doDelete(false);
$("#archive-instead").onclick = () => doDelete(true);
$("#cancel-delete").onclick = () => $("#delete-dialog").close();
window.addEventListener("beforeunload", (event) => {
  if (dirty() || saving) {
    event.preventDefault();
    event.returnValue = "";
  }
});
start().catch((e) => feedback(e.message, true));
