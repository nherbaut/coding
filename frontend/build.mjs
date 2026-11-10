import { build } from "esbuild";
import fs from "node:fs";
import path from "node:path";
const result = await build({
  entryPoints: ["markdown.js"],
  bundle: true,
  minify: true,
  format: "iife",
  globalName: "CorpusMarkdown",
  outfile: "../corpus/static/corpus/markdown.js",
  metafile: true,
  legalComments: "none",
  target: "es2022",
});
const packages = new Set(
  Object.keys(result.metafile.inputs)
    .filter((file) => file.includes("node_modules/"))
    .map((file) => {
      const parts = file.split("node_modules/").at(-1).split("/");
      return parts[0].startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
    }),
);
let notices = "Third-party notices for bundled Markdown and HTML parsers.\n\n";
for (const name of [...packages].sort()) {
  const directory = path.join("node_modules", name),
    info = JSON.parse(fs.readFileSync(path.join(directory, "package.json"))),
    license = fs
      .readdirSync(directory)
      .find((file) => /^licen[sc]e(?:\.|$)/i.test(file));
  notices += `${name} ${info.version} (${info.license})\n${license ? fs.readFileSync(path.join(directory, license), "utf8") : "See package metadata for license terms."}\n\n`;
}
fs.writeFileSync("../corpus/static/corpus/markdown.LICENSE.txt", notices.trimEnd() + "\n");
