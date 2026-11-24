import { cpSync, existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const docs = dirname(fileURLToPath(import.meta.url));
const root = dirname(docs);
const repository = dirname(root);
const content = join(root, ".blume-content");
const locales = ["es", "ja", "ko", "pt-BR", "zh-CN"];
const excluded = new Set([
  ".blume",
  ".blume-content",
  ".blume-verify",
  ".vercel",
  "dist",
  "node_modules",
  "components",
  "pages",
  "blume.config.ts",
  "package-lock.json",
  "package.json",
  "vercel.json",
]);

function prepareContent() {
  rmSync(content, { recursive: true, force: true });
  for (const entry of readdirSync(root)) {
    if (!excluded.has(entry)) cpSync(join(root, entry), join(content, entry), { recursive: true });
  }
  for (const locale of locales) {
    const translatedDocs = join(repository, "i18n", locale, "docs");
    if (existsSync(translatedDocs)) cpSync(translatedDocs, join(content, locale), { recursive: true });
  }

  for (const file of contentFiles(content)) {
    const text = readFileSync(file, "utf8");
    const prepared = normalizeTranslatedTitle(text);
    if (prepared !== text) writeFileSync(file, prepared);
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
      .replaceAll("SQL <-> C++", "SQL ↔ C++");
    text = rewriteSourceLinks(text, relative(content, file));
    writeFileSync(file, text);
  }
}

function contentFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return contentFiles(path);
    return path.endsWith(".md") || path.endsWith(".mdx") ? [path] : [];
  });
}

/** Move a generated translation's title into front matter for Blume to render once. */
export function normalizeTranslatedTitle(text) {
  if (!text.startsWith("<!-- i18n: source=")) return text;

  const lines = text.split("\n");
  const heading = lines.findIndex((line) => /^#\s+/.test(line));
  if (heading === -1) return text;

  const title = lines[heading].replace(/^#\s+/, "").replace(/\s+#+\s*$/, "");
  lines.splice(heading, 1);
  if (lines[heading - 1] === "" && lines[heading] === "") lines.splice(heading, 1);
  return `---\ntitle: ${JSON.stringify(title)}\n---\n\n${lines.join("\n")}`;
}

export function rewriteSourceLinks(text, from) {
  const [locale, ...sourceParts] = from.split("/");
  const sourceFrom = locales.includes(locale) ? sourceParts.join("/") : from;
  let fenced = false;
  return text
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
      if (fenced) return line;
      return line.replace(/(!?\[[^\]]*\]\()([^\s)]+)(\))/g, (whole, prefix, target, suffix) => {
        if (/^(?:[a-z]+:|\/|#)/i.test(target)) return whole;
        const [path, anchor] = target.split("#", 2);
        const resolved = normalize(join(dirname(sourceFrom), decodeURIComponent(path))).replaceAll("\\", "/");
        if (!resolved.startsWith("../")) return whole;
        const repositoryPath = resolved.replace(/^\.\.\//g, "");
        const kind = prefix.startsWith("!") ? "raw" : "blob";
        const fragment = anchor ? `#${anchor}` : "";
        return `${prefix}https://github.com/ClickHouse/loom/${kind}/main/${repositoryPath}${fragment}${suffix}`;
      });
    })
    .join("\n");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) prepareContent();
