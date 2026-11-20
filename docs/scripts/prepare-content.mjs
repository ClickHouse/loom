import { cpSync, existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, relative } from "node:path";
import { spawnSync } from "node:child_process";

const docs = dirname(fileURLToPath(import.meta.url));
const root = dirname(docs);
const content = join(root, ".blume-content");
const excluded = new Set([
  ".blume",
  ".blume-content",
  ".blume-verify",
  ".vercel",
  "dist",
  "node_modules",
  "blume.config.ts",
  "package-lock.json",
  "package.json",
  "vercel.json",
]);

rmSync(content, { recursive: true, force: true });
for (const entry of readdirSync(root)) {
  if (!excluded.has(entry)) cpSync(join(root, entry), join(content, entry), { recursive: true });
}
for (const locale of readdirSync(join(dirname(root), "i18n"))) {
  const translatedRoot = join(dirname(root), "i18n", locale);
  const translatedDocs = join(translatedRoot, "docs");
  if (existsSync(translatedDocs)) cpSync(translatedDocs, join(content, locale), { recursive: true });
}

for (const file of contentFiles(content)) {
  const text = moveI18nNotice(readFileSync(file, "utf8"));
  writeFileSync(file, text);
}

const packagePath = fileURLToPath(import.meta.resolve("blume/package.json"));
const codemod = join(dirname(packagePath), "skills", "blume-migrate", "scripts", "mkdocs-codemod.mjs");
const result = spawnSync(process.execPath, [codemod, "--write", content], {
  cwd: dirname(root),
  stdio: "inherit",
});

if (result.status !== 0) process.exit(result.status ?? 1);
if (!existsSync(join(content, "index.md"))) process.exit("Blume content preparation produced no index page.");

for (const file of contentFiles(content)) {
  let text = readFileSync(file, "utf8")
    .replaceAll("({name, value})", "(`{name, value}`)")
    .replaceAll("({name, clear: true})", "(`{name, clear: true}`)")
    .replaceAll("SQL <-> C++", "SQL ↔ C++")
    .replaceAll('# <img src="logo.svg">', '# &lt;img src="logo.svg"&gt;');
  text = rewriteSourceLinks(text, relative(content, file));
  writeFileSync(file, text);
}

function contentFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return contentFiles(path);
    return path.endsWith(".md") || path.endsWith(".mdx") ? [path] : [];
  });
}

function moveI18nNotice(text) {
  const notice = /^(<!-- i18n:.*? -->\n(?:>.*\n)+\n)/u.exec(text);
  if (!notice) return text;
  const page = text.slice(notice[1].length);
  const heading = /^# .+$/mu.exec(page);
  if (!heading || heading.index === undefined) return text;
  const afterHeading = heading.index + heading[0].length;
  return `${page.slice(0, afterHeading)}\n\n${notice[1].trim()}\n${page.slice(afterHeading)}`;
}

function rewriteSourceLinks(text, from) {
  let fenced = false;
  return text
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
      if (fenced) return line;
      return line.replace(/(!?\[[^\]]*\]\()([^\s)]+)(\))/g, (whole, prefix, target, suffix) => {
        if (/^(?:[a-z]+:|\/|#)/i.test(target)) return whole;
        const [path, anchor] = target.split("#", 2);
        const resolved = normalize(join(dirname(from), decodeURIComponent(path))).replaceAll("\\", "/");
        if (resolved === "README.md") {
          const fragment = anchor ? `#${anchor}` : "";
          return `${prefix}https://github.com/ClickHouse/loom/blob/main/README.md${fragment}${suffix}`;
        }
        if (!resolved.startsWith("../")) return whole;
        const repositoryPath = resolved.replace(/^\.\.\//g, "");
        const kind = prefix.startsWith("!") ? "raw" : "blob";
        const fragment = anchor ? `#${anchor}` : "";
        return `${prefix}https://github.com/ClickHouse/loom/${kind}/main/${repositoryPath}${fragment}${suffix}`;
      });
    })
    .join("\n");
}
