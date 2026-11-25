import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const remote = "https://github.com/ClickHouse/loom.git";
const localizedPages = {
  home: ["HomePage.astro", "home-copy.ts"],
  enterprise: ["EnterprisePage.astro", "enterprise-copy.ts"],
  "knowledge-factory": ["KnowledgeFactoryPage.astro", "knowledge-factory-copy.ts"],
};

function git(cwd, args, input) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
    input,
    maxBuffer: 4 * 1024 * 1024,
    stdio: [input ? "pipe" : "ignore", "pipe", "pipe"],
    timeout: 20_000,
  }).trimEnd();
}

function datesAt(repository, revision) {
  const log = git(repository, [
    "log",
    "--first-parent",
    "--diff-merges=first-parent",
    "--no-renames",
    "--format=%x00%ct",
    "--name-only",
    "-z",
    revision,
    "--",
    "docs",
    "i18n",
  ]);
  const dates = new Map();
  let date;
  let nextIsCommit = false;
  for (const field of log.split("\0")) {
    if (field === "") {
      nextIsCommit = true;
    } else if (nextIsCommit) {
      date = new Date(Number(field) * 1_000).toISOString();
      nextIsCommit = false;
    } else {
      const path = field.replace(/^\n/, "");
      if (!dates.has(path)) dates.set(path, date);
    }
  }
  return dates;
}

function fetchCopyHistory(repository, revision) {
  const copyFiles = Object.values(localizedPages).map(([, file]) => `docs/components/${file}`);
  const log = git(repository, [
    "log",
    "--first-parent",
    "--diff-merges=first-parent",
    "--no-renames",
    "--format=",
    "--raw",
    "--no-abbrev",
    revision,
    "--",
    ...copyFiles,
  ]);
  const blobs = new Set();
  for (const [, before, after] of log.matchAll(/^:\d+ \d+ ([0-9a-f]{40}) ([0-9a-f]{40}) /gm)) {
    for (const blob of [before, after]) {
      if (!/^0+$/.test(blob)) blobs.add(blob);
    }
  }
  if (blobs.size) git(repository, ["fetch", "--no-tags", "--quiet", "--stdin", remote], [...blobs].join("\n") + "\n");
}

function productDatesAt(repository, revision, publicPages, dates) {
  const result = {};
  for (const [route, destination] of Object.entries(publicPages)) {
    if (route === "/adoption") continue; // Live adoption data changes outside Git.
    const page = destination.slice("/docs/".length);
    const [name, locale = "en"] = page.split("/").reverse();
    const pageDate = dates.get(`docs/pages/${page}.astro`);
    if (!pageDate) continue;

    const files = localizedPages[name];
    const updates = [pageDate, dates.get("docs/components/SiteFrame.astro"), dates.get("docs/lib/site-identity.mjs")];
    if (files) {
      const [component, copy] = files;
      const key = locale.includes("-") ? JSON.stringify(locale) : locale;
      const date = git(repository, [
        "log",
        "--first-parent",
        "--format=%cI",
        "--no-patch",
        "-1",
        revision,
        "-L",
        `/^  ${key}: {$/,/^  },$/:docs/components/${copy}`,
      ]);
      if (!date) continue;
      updates.push(new Date(date).toISOString(), dates.get(`docs/components/${component}`));
    }
    result[route] = updates.filter(Boolean).sort().at(-1);
  }
  return result;
}

function siteDatesAt(repository, revision, publicPages, fetchCopies = false) {
  const contentDates = datesAt(repository, revision);
  let productDates = {};
  try {
    if (fetchCopies) fetchCopyHistory(repository, revision);
    productDates = productDatesAt(repository, revision, publicPages, contentDates);
  } catch {
    console.warn("Product page history is unavailable. Omitting product sitemap modification dates.");
  }
  return { contentDates, productDates };
}

export function readSiteDates(repository, publicPages) {
  try {
    if (git(repository, ["rev-parse", "--is-shallow-repository"]) === "false") {
      return siteDatesAt(repository, "HEAD", publicPages);
    }

    const revision = git(repository, ["rev-parse", "HEAD"]);
    const scratch = mkdtempSync(join(tmpdir(), "loom-docs-history-"));
    try {
      // A shallow build needs the trees at its exact revision, with no file blobs.
      git(scratch, ["init", "--bare", "--quiet"]);
      git(scratch, ["fetch", "--filter=blob:none", "--no-tags", "--quiet", remote, revision]);
      return siteDatesAt(scratch, revision, publicPages, true);
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  } catch {
    console.warn("Git history is unavailable. Omitting source modification dates.");
    return { contentDates: new Map(), productDates: {} };
  }
}
