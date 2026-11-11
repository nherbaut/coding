import test from "node:test";
import assert from "node:assert/strict";
import { connect, remove, validate } from "./graph-model.js";
const book = {
  version: 1,
  codes: [
    { id: "a", name: "A", parent: null },
    { id: "b", name: "B", parent: "a" },
    { id: "c", name: "C", parent: "b" },
  ],
};
test("connection replaces parent, disconnect makes root, cycles rejected", () => {
  assert.equal(connect(book, "a", "c").codes[2].parent, "a");
  assert.equal(connect(book, null, "b").codes[1].parent, null);
  assert.throws(() => connect(book, "c", "a"), /cycle/);
  assert.throws(() => connect(book, "b", "b"), /cycle/);
  assert.throws(() => connect(book, "unknown", "b"), /Missing/);
  assert.equal(book.codes[2].parent, "b");
});
test("delete only node reparents children; subtree removes all descendants", () => {
  const only = remove(book, "b", false, /* @__PURE__ */ new Set());
  assert.deepEqual(
    only.codes.map((c) => [c.id, c.parent]),
    [
      ["a", null],
      ["c", "a"],
    ],
  );
  assert.deepEqual(
    remove(book, "b", true, /* @__PURE__ */ new Set()).codes.map((c) => c.id),
    ["a"],
  );
  assert.throws(
    () => remove(book, "a", true, /* @__PURE__ */ new Set(["c"])),
    /Archive/,
  );
  assert.equal(
    remove(book, "a", true, /* @__PURE__ */ new Set(["c"]), true).codes.every(
      (c) => c.archived,
    ),
    true,
  );
  assert.equal(
    remove(book, "b", false, /* @__PURE__ */ new Set(["c"])).codes[1].id,
    "c",
  );
});
test("reparent deletion detects duplicate siblings and never mutates original", () => {
  const source = structuredClone(book);
  source.codes.push({ id: "d", name: "C", parent: "a" });
  assert.throws(
    () => remove(source, "b", false, /* @__PURE__ */ new Set()),
    /Duplicate/,
  );
  assert.equal(source.codes.length, 4);
});
