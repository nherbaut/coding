const clone = (value) => structuredClone(value);
function descendants(book, id) {
  const result = /* @__PURE__ */ new Set([id]);
  let changed;
  do {
    changed = false;
    for (const c of book.codes)
      if (result.has(c.parent) && !result.has(c.id)) {
        result.add(c.id);
        changed = true;
      }
  } while (changed);
  return result;
}
function validate(book) {
  const ids = new Map(book.codes.map((c) => [c.id, c]));
  if (ids.size !== book.codes.length)
    throw Error("Duplicate concept identities");
  const names = /* @__PURE__ */ new Set();
  for (const c of book.codes) {
    if (!c.name.trim()) throw Error("A concept needs a name");
    const name = `${c.parent || ""}\0${c.name.trim().toLowerCase()}`;
    if (names.has(name)) throw Error("Duplicate names under the same parent");
    names.add(name);
    const seen = /* @__PURE__ */ new Set([c.id]);
    let parent = c.parent;
    while (parent) {
      if (!ids.has(parent) || seen.has(parent))
        throw Error("Missing parent or hierarchy cycle");
      seen.add(parent);
      parent = ids.get(parent).parent;
    }
  }
}
function connect(book, parent, child) {
  const result = clone(book),
    concept = result.codes.find((c) => c.id === child);
  if (!concept) throw Error("Choose a child concept");
  concept.parent = parent;
  validate(result);
  return result;
}
function remove(book, id, subtree, referenced, archive = false) {
  const result = clone(book),
    target = result.codes.find((c) => c.id === id);
  if (!target) throw Error("Choose a concept");
  const affected = subtree
    ? descendants(book, id)
    : /* @__PURE__ */ new Set([id]);
  if (archive) {
    for (const c of result.codes) if (affected.has(c.id)) c.archived = true;
    return result;
  }
  if ([...affected].some((i) => referenced.has(i)))
    throw Error("Archive referenced concepts instead of deleting them");
  result.codes = result.codes.filter((c) => !affected.has(c.id));
  if (!subtree) {
    for (const c of result.codes)
      if (c.parent === id) c.parent = target.parent || null;
  }
  validate(result);
  return result;
}
export { clone, connect, descendants, remove, validate };
