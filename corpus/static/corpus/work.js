"use strict";
const $ = (selector) => document.querySelector(selector);
const csrf = $("[name=csrfmiddlewaretoken]").value;
let chosenCodes = new Set();
let state,
  assignment,
  documentVersion,
  selected = null,
  editing = null,
  editingCode = null,
  busy = false;
const status = (message, error = false) => {
  $("#save-status").textContent = message;
  $("#save-status").classList.toggle("error", error);
};
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify(body),
        },
  );
  let data;
  try {
    data = await response.json();
  } catch {
    throw Error("Request failed. Reload your private link if access expired.");
  }
  if (!response.ok) throw Error(data.error || "Access denied");
  return data;
}
async function reload() {
  const previous = assignment?.id,
    doc = documentVersion?.id;
  state = await api("/api/state/");
  assignment =
    state.assignments.find((a) => a.id === previous) || state.assignments[0];
  documentVersion =
    assignment?.documents.find((d) => d.id === doc) || assignment?.documents[0];
  render();
}
async function openDocument(nextAssignment, nextDocument) {
  if (busy) return;
  if (
    !editing &&
    hasUnsavedAnnotation() &&
    !confirm("Discard this unsaved annotation?")
  )
    return;
  clearTimeout(autosaveTimer);
  if (editing && selected) {
    const saved = await saveAnnotation(
      {
        ...selected,
        id: editing,
        codes: [...$("#code-tree").querySelectorAll("input:checked")].map(
          (node) => node.value,
        ),
        memo: $("#memo").value,
      },
      true,
    );
    if (!saved) return;
  }
  assignment = nextAssignment;
  documentVersion = nextDocument;
  selected = null;
  editing = null;
  chosenCodes.clear();
  $("#memo").value = "";
  render();
}
function render() {
  closeAnnotationMenu();
  $("#files").replaceChildren();
  for (const a of state.assignments) {
    const heading = element("h3");
    const caseButton = element(
      "button",
      a.case + (a.locked ? " · Submitted" : ""),
    );
    caseButton.onclick = () => openDocument(a, a.documents[0]);
    heading.append(caseButton);
    $("#files").append(heading);
    for (const d of a.documents) {
      const b = element(
        "button",
        d.name,
        d.id === documentVersion?.id && a.id === assignment?.id ? "active" : "",
      );
      b.onclick = () => openDocument(a, d);
      $("#files").append(b);
    }
  }
  $("#case-title").textContent = assignment?.case || "No cases assigned";
  $("#document-title").textContent =
    documentVersion?.name ||
    (assignment
      ? "This case has no text documents."
      : "Your admin can assign cases to this session.");
  $("#metadata").textContent = JSON.stringify(
    documentVersion?.metadata || {},
    null,
    2,
  );
  $("#submit-case").disabled = !assignment || assignment.locked || busy;
  $("#save-annotation").disabled = !assignment || assignment.locked || busy;
  $("#memo").disabled = !!assignment?.locked;
  $("#selection").textContent = selected?.selected_text || "";
  $("#cancel-edit").hidden = !editing;
  renderText();
  renderCodes();
  renderAnnotations();
  renderDraftStatus();
}
function applyCodeColors(node, ids) {
  const colors = [
    ...new Set(
      ids
        .sort()
        .map((id) => state.colors[id])
        .filter(Boolean),
    ),
  ];
  if (!colors.length) return;
  const stops = colors.flatMap((color, index) => {
    const tint = `color-mix(in srgb, ${color} 40%, white)`;
    return [
      `${tint} ${(index * 100) / colors.length}%`,
      `${tint} ${((index + 1) * 100) / colors.length}%`,
    ];
  });
  node.style.background = `linear-gradient(to bottom, ${stops.join(",")})`;
  node.style.color = "#24332e";
}
function hasUnsavedAnnotation() {
  if (!selected) return false;
  if (!editing) return true;
  const saved = assignment.annotations.find((item) => item.id === editing);
  return (
    !saved ||
    saved.memo !== $("#memo").value ||
    JSON.stringify([...saved.codes].sort()) !==
      JSON.stringify([...chosenCodes].sort())
  );
}
function renderDraftStatus() {
  const count =
    assignment?.annotations.filter(
      (item) => item.document === documentVersion?.id,
    ).length || 0;
  $("#annotation-count").textContent =
    `${count} saved annotation${count === 1 ? "" : "s"}`;
  const pending = hasUnsavedAnnotation();
  $("#annotation-guidance").textContent = assignment?.locked
    ? "Submitted case. Ask the admin to reopen it for changes."
    : pending
      ? chosenCodes.size
        ? "Unsaved annotation. Click Save annotation to keep this highlight."
        : "Choose a code, then click Save annotation."
      : "";
  $("#selection").hidden = !selected || !chosenCodes.size;
  $("#selection").textContent =
    selected && chosenCodes.size ? selected.selected_text : "";
  $("#annotation-guidance").hidden = !$("#annotation-guidance").textContent;
  $("#save-annotation").disabled =
    !selected || !chosenCodes.size || !!assignment?.locked || busy;
  $("#cancel-edit").hidden = !selected;
  $("#cancel-edit").textContent = editing ? "Cancel edit" : "Clear selection";
}
function renderText() {
  const root = $("#document");
  root.replaceChildren();
  if (!documentVersion) return;
  const text = documentVersion.text;
  let annotations = assignment.annotations.filter(
    (a) => a.document === documentVersion.id,
  );
  if (selected?.document === documentVersion.id && hasUnsavedAnnotation()) {
    annotations = annotations.filter((item) => item.id !== editing);
    annotations.push({ ...selected, codes: [...chosenCodes], preview: true });
  }
  const points = [
    ...new Set([
      0,
      text.length,
      ...annotations.flatMap((a) => [a.start, a.end]),
    ]),
  ].sort((a, b) => a - b);
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i],
      end = points[i + 1];
    const active = annotations.filter((a) => a.start < end && a.end > start);
    if (active.length) {
      const mark = element("mark", text.slice(start, end));
      if (active.some((item) => item.preview && !item.codes.length)) {
        mark.classList.add("annotation-selection");
      } else {
        applyCodeColors(
          mark,
          active.flatMap((item) => item.codes),
        );
      }
      if (active.some((item) => item.preview))
        mark.classList.add("annotation-preview");
      mark.title = active
        .flatMap((a) =>
          a.codes.map(
            (id) => state.codebook.codes.find((c) => c.id === id)?.name || id,
          ),
        )
        .join(", ");
      const saved = active.filter((item) => !item.preview);
      if (saved.length && !active.some((item) => item.preview)) {
        mark.classList.add("annotation-highlight");
        mark.tabIndex = 0;
        mark.setAttribute("role", "button");
        mark.setAttribute("aria-label", `Edit annotation memo: ${mark.title}`);
        mark.title +=
          " — Click to view or edit memos; right-click for annotation actions";
        mark.onclick = () => openAnnotationMemos(saved);
        mark.oncontextmenu = (event) => {
          event.preventDefault();
          openAnnotationMenu(saved, event.clientX, event.clientY, mark);
        };
        mark.onkeydown = (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openAnnotationMemos(saved);
          } else if (
            event.key === "ContextMenu" ||
            (event.shiftKey && event.key === "F10")
          ) {
            event.preventDefault();
            const rect = mark.getBoundingClientRect();
            openAnnotationMenu(saved, rect.left, rect.bottom, mark);
          }
        };
      }
      root.append(mark);
    } else root.append(document.createTextNode(text.slice(start, end)));
  }
}
const termOrder = new Intl.Collator("en", {
  sensitivity: "base",
  numeric: true,
});
function hierarchyEntries(codes, excludedId = null) {
  const entries = [];
  function visit(parent, depth, ancestors) {
    const children = codes
      .filter((code) => (code.parent || null) === parent)
      .sort(
        (a, b) => termOrder.compare(a.name, b.name) || a.id.localeCompare(b.id),
      );
    for (const code of children) {
      if (code.id === excludedId) continue;
      const path = [...ancestors, code.name];
      entries.push({ code, depth, path: path.join(" › ") });
      visit(code.id, depth + 1, path);
    }
  }
  visit(null, 0, []);
  return entries;
}
function bounceSaveButton() {
  const button = $("#save-annotation");
  for (const animation of button.getAnimations()) animation.cancel();
  if (
    button.disabled ||
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  )
    return;
  button.animate(
    [
      { transform: "translateY(0)" },
      { transform: "translateY(-6px)" },
      { transform: "translateY(0)" },
      { transform: "translateY(-2px)" },
      { transform: "translateY(0)" },
    ],
    { duration: 380, easing: "ease-out" },
  );
}
function renderCodes() {
  const root = $("#code-tree");
  const chosen = chosenCodes;
  root.replaceChildren();
  const search = $("#code-search").value.toLowerCase();
  for (const { code, depth } of hierarchyEntries(state.codebook.codes)) {
    if (
      (!code.archived || (editing && chosen.has(code.id))) &&
      (!search || code.name.toLowerCase().includes(search))
    ) {
      const row = element("div", undefined, "code-row");
      row.style.paddingLeft = Math.min(depth, 6) * 12 + "px";
      const label = element("label");
      if (code.kind === "code") {
        const input = element("input");
        input.type = "checkbox";
        input.value = code.id;
        input.checked = chosen.has(code.id);
        input.disabled = !selected || !!assignment?.locked || !!code.archived;
        input.onchange = () => {
          if (input.checked) chosenCodes.add(code.id);
          else chosenCodes.delete(code.id);
          renderText();
          renderDraftStatus();
          if (input.checked) bounceSaveButton();
        };
        label.append(input);
        const swatch = element("span", undefined, "code-swatch");
        swatch.style.backgroundColor = state.colors[code.id];
        swatch.setAttribute("aria-hidden", "true");
        label.append(swatch);
      }
      label.append(document.createTextNode(code.name));
      label.title = code.memo;
      row.append(label);
      const edit = element("button", "⋯", "secondary");
      edit.setAttribute("aria-label", "Edit " + code.name);
      edit.onclick = () => openCode(code);
      row.append(edit);
      root.append(row);
    }
  }
}
function renderAnnotations() {
  const root = $("#annotations");
  root.replaceChildren();
  for (const a of assignment?.annotations.filter(
    (a) => a.document === documentVersion?.id,
  ) || []) {
    const card = element("article", undefined, "annotation");
    const quote = element("blockquote", a.selected_text);
    applyCodeColors(quote, a.codes);
    card.append(quote);
    for (const id of a.codes) {
      const tag = element(
        "span",
        state.codebook.codes.find((c) => c.id === id)?.name || id,
        "tag",
      );
      applyCodeColors(tag, [id]);
      card.append(tag);
    }
    if (a.memo) card.append(element("p", a.memo));
    if (!assignment.locked) {
      const memoButton = element("button", "Edit memo", "secondary");
      memoButton.onclick = () => openAnnotationMemos([a]);
      card.append(memoButton);
      const edit = element("button", "Edit", "secondary");
      edit.onclick = () => {
        editing = a.id;
        selected = { ...a };
        chosenCodes = new Set(a.codes);
        $("#memo").value = a.memo;
        render();
      };
      const remove = element("button", "Delete", "secondary");
      remove.onclick = () => deleteSavedAnnotation(a);
      card.append(edit, remove);
    }
    root.append(card);
  }
}
let annotationMenuAnchor = null;
function closeAnnotationMenu(restoreFocus = false) {
  $("#annotation-context-menu").hidden = true;
  if (restoreFocus && annotationMenuAnchor?.isConnected)
    annotationMenuAnchor.focus();
  annotationMenuAnchor = null;
}
function deleteSavedAnnotation(annotation) {
  if (busy) return;
  if (assignment.locked) {
    status(
      "The admin must reopen this case before annotations can be deleted.",
      true,
    );
    return;
  }
  if (hasUnsavedAnnotation()) {
    status(
      "Save or clear the current annotation before deleting another annotation.",
      true,
    );
    return;
  }
  return saveAnnotation({ id: annotation.id, delete: true });
}
function openAnnotationMenu(annotations, x, y, anchor) {
  closeAnnotationMenu();
  if (busy || hasUnsavedAnnotation()) {
    status(
      "Save or clear the current annotation before opening annotation actions.",
      true,
    );
    return;
  }
  const menu = $("#annotation-context-menu");
  menu.replaceChildren();
  annotationMenuAnchor = anchor;
  for (const annotation of annotations) {
    const group = element("div", undefined, "annotation-menu-group");
    group.dataset.annotationId = annotation.id;
    const names = annotation.codes
      .map(
        (id) => state.codebook.codes.find((code) => code.id === id)?.name || id,
      )
      .join(", ");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", names);
    if (annotations.length > 1) {
      group.append(element("strong", names));
      group.append(element("small", annotation.selected_text.slice(0, 80)));
    }
    const editMemo = element("button", "Edit memo");
    editMemo.type = "button";
    editMemo.setAttribute("role", "menuitem");
    editMemo.disabled = assignment.locked;
    editMemo.onclick = () => {
      closeAnnotationMenu();
      openAnnotationMemos([annotation]);
    };
    const remove = element("button", "Delete annotation");
    remove.type = "button";
    remove.setAttribute("role", "menuitem");
    remove.className = "delete-annotation";
    remove.disabled = assignment.locked;
    remove.onclick = () => {
      closeAnnotationMenu();
      deleteSavedAnnotation(annotation);
    };
    group.append(editMemo, remove);
    menu.append(group);
  }
  menu.hidden = false;
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  menu.querySelector("button:not(:disabled)")?.focus();
}
document.addEventListener("pointerdown", (event) => {
  if (!$("#annotation-context-menu").contains(event.target))
    closeAnnotationMenu();
});
document.addEventListener("keydown", (event) => {
  const menu = $("#annotation-context-menu");
  if (menu.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeAnnotationMenu(true);
    return;
  }
  if (event.key === "Tab") {
    closeAnnotationMenu();
    return;
  }
  const items = [...menu.querySelectorAll("button:not(:disabled)")];
  if (!items.length) return;
  const current = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    items[
      (current +
        (event.key === "ArrowDown" ? 1 : items.length - 1) +
        items.length) %
        items.length
    ].focus();
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    items[event.key === "Home" ? 0 : items.length - 1].focus();
  }
});
window.addEventListener("resize", () => closeAnnotationMenu());
document.addEventListener(
  "scroll",
  (event) => {
    if (!$("#annotation-context-menu").contains(event.target))
      closeAnnotationMenu();
  },
  true,
);
function openAnnotationMemos(annotations) {
  if (busy) {
    status("Wait for the current save to finish.", true);
    return;
  }
  if (hasUnsavedAnnotation()) {
    status("Save or clear the current annotation before editing a memo.", true);
    return;
  }
  const owner = assignment;
  const root = $("#memo-editors");
  root.replaceChildren();
  for (const annotation of annotations) {
    const card = element("article", undefined, "memo-editor");
    const names = annotation.codes.map(
      (id) => state.codebook.codes.find((code) => code.id === id)?.name || id,
    );
    card.append(element("h3", names.join(", ")));
    const quote = element("blockquote", annotation.selected_text);
    applyCodeColors(quote, annotation.codes);
    card.append(quote);
    const label = element("label", "Memo");
    const input = element("textarea");
    input.rows = 5;
    input.value = annotation.memo;
    input.readOnly = owner.locked;
    label.append(input);
    card.append(label);
    const feedback = element(
      "p",
      owner.locked
        ? "Submitted case. The admin must reopen it to edit memos."
        : "",
      "memo-save-status",
    );
    feedback.setAttribute("role", "status");
    const save = element("button", "Save memo");
    save.type = "button";
    save.disabled = owner.locked;
    save.onclick = async () => {
      if (busy) return;
      busy = true;
      const memo = input.value;
      input.readOnly = true;
      for (const button of root.querySelectorAll("button"))
        button.disabled = true;
      feedback.textContent = "Saving…";
      feedback.classList.remove("error");
      try {
        const result = await api(`/api/assignments/${owner.id}/annotations/`, {
          ...annotation,
          revision: owner.revision,
          codebook_revision: state.revision,
          memo,
        });
        owner.revision = result.revision;
        owner.annotations = result.annotations;
        if (editing === annotation.id) {
          selected = {
            ...owner.annotations.find((item) => item.id === annotation.id),
          };
          $("#memo").value = memo;
        }
        feedback.textContent = "Memo saved";
        status("Memo saved");
      } catch (error) {
        feedback.textContent = error.message;
        feedback.classList.add("error");
      } finally {
        busy = false;
        input.readOnly = owner.locked;
        for (const button of root.querySelectorAll("button"))
          button.disabled = owner.locked;
        render();
      }
    };
    card.append(save, feedback);
    root.append(card);
  }
  $("#memo-dialog").showModal();
}
function captureSelection(event) {
  if (event.type === "mouseup" && event.button !== 0) return;
  if (assignment?.locked) return;
  const selection = window.getSelection();
  if (!selection.rangeCount || selection.isCollapsed) return;
  const range = selection.getRangeAt(0),
    root = $("#document");
  if (
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  )
    return;
  const backward =
    selection.anchorNode === range.endContainer &&
    selection.anchorOffset === range.endOffset;
  const prefix = range.cloneRange();
  prefix.selectNodeContents(root);
  prefix.setEnd(range.startContainer, range.startOffset);
  const start = prefix.toString().length,
    end = start + range.toString().length;
  selected = {
    document: documentVersion.id,
    document_hash: documentVersion.hash,
    start,
    end,
    selected_text: documentVersion.text.slice(start, end),
  };
  editing = null;
  chosenCodes.clear();
  $("#memo").value = "";
  renderCodes();
  renderText();
  const marks = root.querySelectorAll("mark.annotation-selection");
  if (marks.length) {
    const first = marks[0].firstChild;
    const last = marks[marks.length - 1].firstChild;
    if (backward) selection.setBaseAndExtent(last, last.length, first, 0);
    else selection.setBaseAndExtent(first, 0, last, last.length);
  }
  renderDraftStatus();
}
$("#document").addEventListener("mouseup", captureSelection);
$("#document").addEventListener("keyup", captureSelection);
async function saveAnnotation(extra, preserveEditor = false) {
  if (busy) return;
  const targetAssignment = assignment;
  busy = true;
  status("Saving…");
  try {
    const result = await api(
      `/api/assignments/${targetAssignment.id}/annotations/`,
      {
        revision: targetAssignment.revision,
        codebook_revision: state.revision,
        ...extra,
      },
    );
    targetAssignment.revision = result.revision;
    targetAssignment.annotations = result.annotations;
    if (!preserveEditor && assignment === targetAssignment) {
      selected = null;
      editing = null;
      chosenCodes.clear();
      $("#memo").value = "";
    }
    status("All changes saved");
    return true;
  } catch (e) {
    status(e.message, true);
    return false;
  } finally {
    busy = false;
    render();
  }
}
$("#save-annotation").onclick = () => {
  if (!selected) {
    status("Select a passage first", true);
    return;
  }
  saveAnnotation({
    ...selected,
    id: editing,
    codes: [...chosenCodes],
    memo: $("#memo").value,
  });
};
$("#cancel-edit").onclick = () => {
  editing = null;
  selected = null;
  chosenCodes.clear();
  $("#memo").value = "";
  render();
};
$("#code-search").oninput = renderCodes;
$("#submit-case").onclick = async () => {
  if (hasUnsavedAnnotation()) {
    status(
      "Save or clear the current annotation before submitting the case.",
      true,
    );
    return;
  }
  if (busy || !confirm("Submit and lock this case for admin review?")) return;
  busy = true;
  try {
    await api(`/api/assignments/${assignment.id}/submit/`, {
      revision: assignment.revision,
    });
    status("Case submitted");
    await reload();
  } catch (e) {
    status(e.message, true);
  } finally {
    busy = false;
    render();
  }
};
$("#open-book").onclick = () => {
  $("#book-yaml").value = state.yaml;
  $("#book-status").textContent = "";
  $("#book-dialog").showModal();
};
$("#save-book").onclick = async () => {
  try {
    await api("/api/codebook/", {
      revision: state.revision,
      yaml: $("#book-yaml").value,
    });
    await reload();
    $("#book-dialog").close();
    status("Codebook saved");
  } catch (e) {
    $("#book-status").textContent = e.message;
  }
};
function openCode(code) {
  editingCode = code?.id || null;
  $("#code-dialog-title").textContent = code ? "Edit concept" : "Add code";
  $("#code-name").value = code?.name || "";
  $("#code-kind").value = code?.kind || "code";
  const palette = [
    "#e9a8b4",
    "#a8c7e9",
    "#c3a8e9",
    "#a8e9b5",
    "#e9c8a8",
    "#e9e0a8",
    "#a8e2e9",
    "#dfb0db",
  ];
  $("#code-color").value = code
    ? state.colors[code.id] || "#d8e4cf"
    : palette[
        state.codebook.codes.filter((c) => c.kind === "code").length %
          palette.length
      ];
  $("#code-memo").value = code?.memo || "";
  $("#code-rationale").value = code?.rationale || "";
  $("#code-archived").checked = !!code?.archived;
  $("#code-evidence").checked = false;
  $("#code-evidence").disabled = !selected;
  $("#code-status").textContent = "";
  $("#delete-code").hidden = !code;
  const parent = $("#code-parent");
  parent.replaceChildren();
  const option = element("option", "No parent");
  option.value = "";
  parent.append(option);
  for (const { code: term, depth, path } of hierarchyEntries(
    state.codebook.codes,
    editingCode,
  )) {
    const kind = term.kind === "category" ? "Category" : "Code";
    const option = element(
      "option",
      `${"\u00a0\u00a0".repeat(depth)}${term.name} (${kind})`,
    );
    option.value = term.id;
    option.title = path;
    parent.append(option);
  }
  parent.value = code?.parent || "";
  $("#code-dialog").showModal();
}
$("#new-code").onclick = () => openCode(null);
$("#close-code").onclick = () => $("#code-dialog").close();
async function saveConcept(book) {
  await api("/api/codebook/", { revision: state.revision, codebook: book });
  await reload();
  $("#code-dialog").close();
  status("Codebook saved");
}
$("#code-form").onsubmit = async (event) => {
  event.preventDefault();
  const book = structuredClone(state.codebook);
  let code = book.codes.find((c) => c.id === editingCode);
  if (!code) {
    code = { id: crypto.randomUUID(), references: [] };
    book.codes.push(code);
  }
  Object.assign(code, {
    name: $("#code-name").value,
    kind: $("#code-kind").value,
    color: $("#code-color").value,
    parent: $("#code-parent").value || null,
    memo: $("#code-memo").value,
    rationale: $("#code-rationale").value,
    archived: $("#code-archived").checked,
  });
  if ($("#code-evidence").checked && selected) {
    code.references.push({
      type: "corpus",
      document: selected.document,
      hash: selected.document_hash || documentVersion.hash,
      start: selected.start,
      end: selected.end,
      quote: selected.selected_text,
    });
  }
  try {
    await saveConcept(book);
  } catch (e) {
    $("#code-status").textContent = e.message;
  }
};
$("#delete-code").onclick = async () => {
  if (!confirm("Delete this unused concept?")) return;
  const book = structuredClone(state.codebook);
  book.codes = book.codes.filter((c) => c.id !== editingCode);
  try {
    await saveConcept(book);
  } catch (e) {
    $("#code-status").textContent = e.message;
  }
};
reload().catch((e) => status(e.message, true));
// Changes to an existing annotation save after a brief pause.
let autosaveTimer;
function scheduleAutosave() {
  clearTimeout(autosaveTimer);
  if (!editing || !selected || assignment?.locked) return;
  const identity = editing,
    caseId = assignment.id;
  status("Unsaved changes…");
  autosaveTimer = setTimeout(() => {
    if (editing === identity && assignment.id === caseId && !busy) {
      saveAnnotation(
        {
          ...selected,
          id: editing,
          codes: [...chosenCodes],
          memo: $("#memo").value,
        },
        true,
      );
    } else if (editing === identity && assignment.id === caseId) {
      scheduleAutosave();
    }
  }, 900);
}
$("#memo").addEventListener("input", () => {
  renderText();
  renderDraftStatus();
  scheduleAutosave();
});
$("#code-tree").addEventListener("change", scheduleAutosave);
