#!/usr/bin/env node
/*
 * Regenerate the operator manual .docx from the HTML master.
 *
 * user-guide.html is the single source of truth; this script converts it so
 * the Word copy never drifts from it by hand. Run it after every doc edit:
 *
 *     cd docs && npm install html-to-docx && node build-docx.js
 *
 * The one dependency (html-to-docx) is intentionally not vendored — the docs
 * are edited rarely and the install is a few seconds. Behind the corporate
 * proxy, point Node at the trust store first:
 *   NODE_EXTRA_CA_CERTS=<...>/windows-ca-bundle.pem node build-docx.js
 */

const fs = require("fs");
const path = require("path");
const HTMLtoDOCX = require("html-to-docx");

const HERE = __dirname;
const SOURCE = path.join(HERE, "user-guide.html");
const OUTPUT = path.join(HERE, "Fulcrum-Ops-Operator-Manual.docx");

async function main() {
  const html = fs.readFileSync(SOURCE, "utf8");

  // Strip the page's own <style>: html-to-docx maps structural tags to Word
  // styles, and the screen CSS (dark theme, sticky TOC) only confuses it.
  const body = html.replace(/<style[\s\S]*?<\/style>/gi, "");

  const buffer = await HTMLtoDOCX(body, null, {
    title: "Fulcrum Ops — Operator Manual",
    orientation: "portrait",
    margins: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
    table: { row: { cantSplit: true } },
    footer: true,
    pageNumber: true,
    font: "Calibri",
    fontSize: 22,
  });

  fs.writeFileSync(OUTPUT, buffer);
  const kb = (fs.statSync(OUTPUT).size / 1024).toFixed(1);
  console.log(`wrote ${path.basename(OUTPUT)} (${kb} KB) from ${path.basename(SOURCE)}`);
}

main().catch((err) => {
  console.error("build-docx failed:", err.message);
  process.exit(1);
});
