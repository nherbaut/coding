import test from "node:test";
import assert from "node:assert/strict";
import { parse, leafUnits, unitsFor, isMarkdown } from "./markdown.js";
const leaves = (source) => {
  const result = [];
  function walk(node) {
    if (["text", "code", "inlineCode"].includes(node.type)) result.push(node);
    node.children?.forEach(walk);
  }
  walk(parse(source));
  return result;
};
test("Markdown leaves preserve source spans across Unicode, escapes and entities", () => {
  const source =
    "# Title\n\nA **bold😀** &amp; \\*literal\\* [link](https://example.org).";
  for (const node of leaves(source)) {
    const units = leafUnits(node, source);
    assert.equal(units.map((u) => u.value).join(""), node.value);
    for (const unit of units) {
      assert.ok(
        unit.start >= node.position.start.offset &&
          unit.end <= node.position.end.offset,
      );
    }
  }
  const entity = unitsFor("&amp; &#x1f600;");
  assert.equal(entity.map((u) => u.value).join(""), "& 😀");
  assert.deepEqual(entity.at(-1), { value: "\ude00", start: 6, end: 15 });
});
test("List and blockquote prefixes are excluded without guessing text offsets", () => {
  for (const source of [
    "> first\n> second &amp; third",
    "- first\n  continued",
    "> - first\n>   continued",
  ])
    for (const node of leaves(source))
      assert.equal(
        leafUnits(node, source)
          .map((u) => u.value)
          .join(""),
        node.value,
      );
});
test("Inline and fenced code map only content, including CRLF and indentation", () => {
  for (const source of [
    "` a  b `",
    "`a\r\nb`",
    "```js\r\na😀\r\nb\r\n```",
    "    a\n    b",
    "> ```\n> code\n> ```",
  ])
    for (const node of leaves(source)) {
      const units = leafUnits(node, source);
      assert.equal(
        units.map((u) => u.value).join(""),
        node.type === "inlineCode"
          ? node.value.replace(/\r\n|\r|\n/g, " ")
          : node.value,
      );
      for (const unit of units)
        assert.ok(
          source.slice(unit.start, unit.end).includes(unit.value) ||
            unit.value === " ",
        );
    }
});
test("Markdown detection supports names and source metadata", () => {
  assert.equal(isMarkdown("README.MD"), true);
  assert.equal(isMarkdown("Policy", { path: "docs/policy.markdown" }), true);
  assert.equal(isMarkdown("README.txt"), false);
  assert.equal(isMarkdown("Policy", {}, "# Policy\n\n**Required**"), true);
});

test("Large single-line passages and empty code fences retain correct positions", () => {
  const source = "x".repeat(200000);
  const mapped = leafUnits(leaves(source)[0], source);
  assert.equal(mapped.length, source.length);
  assert.equal(mapped.at(-1).end, source.length);
  assert.deepEqual(leafUnits(leaves("```\n```")[0], "```\n```"), []);
  assert.equal(
    unitsFor("\\*", 0, true, false, false)
      .map((u) => u.value)
      .join(""),
    "\\*",
  );
});
