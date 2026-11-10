// The shell evaluates what it shows — a budget on the index chunk. Builds with source
// maps into a scratch directory, attributes the index chunk's bytes to source files, and
// fails when the chunk is over budget or carries a surface that opens on a click (a flow
// dialog, the header search's body, a view), or when the stylesheets or the scripts every
// page loads at boot are over their budgets. Run from frontend/: node scripts/bundle_budget.mjs
import { execSync } from "node:child_process";
import fs from "node:fs";
import zlib from "node:zlib";
import path from "node:path";
import { TraceMap, originalPositionFor } from "@jridgewell/trace-mapping";

const INDEX_BUDGET_BYTES = 120 * 1024;
// The stylesheets block first paint on every route; gzipped, as served: Click UI's light
// tokens and the shell's components (~23 kB) and Loom's (~14 kB).
const CSS_BUDGET_GZIP_BYTES = 50 * 1024;
// Every script the first paint waits on (the entry and its preloads), gzipped: React and
// Radix (~88 kB), the Click UI the shell uses (~64 kB, its flag, logo and payment maps
// aliased away in vite.config.ts) and the app (~34 kB).
const BOOT_JS_BUDGET_GZIP_BYTES = 198 * 1024;
// Sources that must not evaluate on every boot (relative to frontend/src).
const FORBIDDEN_IN_INDEX = [/^flows\/.*Dialog\.tsx$/, /^components\/HeaderSearch\.tsx$/, /^views\/(?!registry\.ts$).+/];

const out = path.resolve("dist-budget");
fs.rmSync(out, { recursive: true, force: true });
execSync(`npx vite build --sourcemap --outDir ${JSON.stringify(out)} --emptyOutDir`, { stdio: "inherit" });
const assets = path.join(out, "assets");
// The entry is the module script index.html loads, not the first index-*.js on disk: other
// modules named index (a Radix package's) become chunks with the same prefix.
const html = fs.readFileSync(path.join(out, "index.html"), "utf8");
const index = [...html.matchAll(/<script\b[^>]*>/g)]
  .map(([tag]) => /\btype="module"/.test(tag) && /\bsrc="[^"]*\/assets\/(index-[^"/]+\.js)"/.exec(tag))
  .find(Boolean)?.[1];
if (!index) {
  console.error("bundle-budget: no module entry script in index.html");
  process.exit(2);
}
const code = fs.readFileSync(path.join(assets, index), "utf8");
const map = new TraceMap(JSON.parse(fs.readFileSync(path.join(assets, `${index}.map`), "utf8")));
const by = new Map();
const lines = code.split("\n");
for (let li = 0; li < lines.length; li++) {
  const L = lines[li];
  let prevCol = 0;
  for (let c = 16; c <= L.length + 16; c += 16) {
    const col = Math.min(c, L.length);
    const p = originalPositionFor(map, { line: li + 1, column: Math.max(0, col - 16) });
    let src = p.source || "(unmapped)";
    const i = src.lastIndexOf("/src/");
    if (i >= 0) src = src.slice(i + 5);
    else if (src.includes("node_modules/")) src = "node_modules";
    by.set(src, (by.get(src) || 0) + (col - prevCol));
    prevCol = col;
    if (col === L.length) break;
  }
}
const problems = [];
// lib/staleChunk.ts skips its reload for the error wording's chunk by the file's name
// (WORDING_CHUNK): the build must keep that name, or every browser reloads on its failure.
if (!fs.readdirSync(assets).some((f) => /^errorWords-[\w-]+\.js$/.test(f)))
  problems.push("no errorWords-*.js chunk: lib/staleChunk.ts WORDING_CHUNK no longer names the wording's file");
if (code.length > INDEX_BUDGET_BYTES)
  problems.push(`index chunk ${(code.length / 1024).toFixed(1)} kB over the ${INDEX_BUDGET_BYTES / 1024} kB budget`);
const css = [...html.matchAll(/<link rel="stylesheet"[^>]*href="[^"]*\/assets\/([^"/]+\.css)"/g)].map((m) => m[1]);
const cssGzip = css.reduce((n, f) => n + zlib.gzipSync(fs.readFileSync(path.join(assets, f)), { level: 9 }).length, 0);
console.log(`bundle-budget: stylesheets ${(cssGzip / 1024).toFixed(1)} kB gzipped (${css.join(", ")})`);
const boot = [
  index,
  ...[...html.matchAll(/<link rel="modulepreload"[^>]*href="[^"]*\/assets\/([^"/]+\.js)"/g)].map((m) => m[1]),
];
const bootGzip = boot.reduce(
  (n, f) => n + zlib.gzipSync(fs.readFileSync(path.join(assets, f)), { level: 9 }).length,
  0,
);
console.log(`bundle-budget: boot scripts ${(bootGzip / 1024).toFixed(1)} kB gzipped (${boot.join(", ")})`);
if (bootGzip > BOOT_JS_BUDGET_GZIP_BYTES)
  problems.push(
    `boot scripts ${(bootGzip / 1024).toFixed(1)} kB gzipped over the ${BOOT_JS_BUDGET_GZIP_BYTES / 1024} kB budget`,
  );
if (cssGzip > CSS_BUDGET_GZIP_BYTES)
  problems.push(
    `stylesheets ${(cssGzip / 1024).toFixed(1)} kB gzipped over the ${CSS_BUDGET_GZIP_BYTES / 1024} kB budget`,
  );
for (const [src, n] of by)
  if (n >= 256 && FORBIDDEN_IN_INDEX.some((re) => re.test(src)))
    problems.push(`${src} (${(n / 1024).toFixed(1)} kB) evaluates on every boot — load it when opened`);
console.log(`bundle-budget: index ${index} ${(code.length / 1024).toFixed(1)} kB; top sources:`);
for (const [s, n] of [...by.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12))
  console.log(`   ${(n / 1024).toFixed(1).padStart(6)} kB  ${s}`);
fs.rmSync(out, { recursive: true, force: true });
if (problems.length) {
  for (const p of problems) console.error(`bundle-budget: ${p}`);
  process.exit(1);
}
console.log("bundle-budget: OK");
