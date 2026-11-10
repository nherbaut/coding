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
  $("#memo").value = "";
  render();
}
function render() {
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
  $("#selection").textContent =
    selected?.selected_text || "Select a passage in the document.";
  $("#cancel-edit").hidden = !editing;
  renderText();
  renderCodes();
  renderAnnotations();
}
function renderText() {
  const root = $("#document");
  root.replaceChildren();
  if (!documentVersion) return;
  const text = documentVersion.text;
  const annotations = assignment.annotations.filter(
    (a) => a.document === documentVersion.id,
  );
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
      mark.title = active
        .flatMap((a) =>
          a.codes.map(
            (id) => state.codebook.codes.find((c) => c.id === id)?.name || id,
          ),
        )
        .join(", ");
      root.append(mark);
    } else root.append(document.createTextNode(text.slice(start, end)));
  }
}
function renderCodes() {
  const root = $("#code-tree");
  const chosen = chosenCodes;
  root.replaceChildren();
  const search = $("#code-search").value.toLowerCase();
  const codes = state.codebook.codes;
  function append(parent, depth) {
    for (const code of codes.filter((c) => (c.parent || null) === parent)) {
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
          input.disabled = !!assignment?.locked || !!code.archived;
          input.onchange = () => {
            if (input.checked) chosenCodes.add(code.id);
            else chosenCodes.delete(code.id);
          };
          label.append(input);
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
      append(code.id, depth + 1);
    }
  }
  append(null, 0);
}
function renderAnnotations() {
  const root = $("#annotations");
  root.replaceChildren();
  for (const a of assignment?.annotations.filter(
    (a) => a.document === documentVersion?.id,
  ) || []) {
    const card = element("article", undefined, "annotation");
    card.append(element("blockquote", a.selected_text));
    for (const id of a.codes)
      card.append(
        element(
          "span",
          state.codebook.codes.find((c) => c.id === id)?.name || id,
          "tag",
        ),
      );
    if (a.memo) card.append(element("p", a.memo));
    if (!assignment.locked) {
      const edit = element("button", "Edit", "secondary");
      edit.onclick = () => {
        editing = a.id;
        selected = { ...a };
        chosenCodes = new Set(a.codes);
        $("#memo").value = a.memo;
        render();
      };
      const remove = element("button", "Delete", "secondary");
      remove.onclick = () => saveAnnotation({ id: a.id, delete: true });
      card.append(edit, remove);
    }
    root.append(card);
  }
}
function captureSelection() {
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
  const active = new Set(
    state.codebook.codes
      .filter((code) => !code.archived)
      .map((code) => code.id),
  );
  chosenCodes = new Set([...chosenCodes].filter((id) => active.has(id)));
  renderCodes();
  $("#selection").textContent = selected.selected_text;
  $("#cancel-edit").hidden = true;
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
  $("#memo").value = "";
  render();
};
$("#code-search").oninput = renderCodes;
$("#submit-case").onclick = async () => {
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
  for (const c of state.codebook.codes.filter((c) => c.id !== editingCode)) {
    const o = element("option", c.name);
    o.value = c.id;
    parent.append(o);
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
$("#memo").addEventListener("input", scheduleAutosave);
$("#code-tree").addEventListener("change", scheduleAutosave);
