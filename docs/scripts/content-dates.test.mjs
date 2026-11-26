import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { readSiteDates } from "./content-dates.mjs";

test("dates a localized page from page-specific changes and skips ambiguous shared edits", (t) => {
  const repository = mkdtempSync(join(tmpdir(), "loom-product-page-dates-"));
  t.after(() => rmSync(repository, { recursive: true, force: true }));

  function git(args, date) {
    return execFileSync("git", args, {
      cwd: repository,
      env: { ...process.env, GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date },
      stdio: "pipe",
    });
  }

  function commit(date) {
    git(["add", "docs"]);
    git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "Update page"], date);
  }

  git(["init", "--quiet"]);
  mkdirSync(join(repository, "docs/pages/pt-BR"), { recursive: true });
  mkdirSync(join(repository, "docs/components"), { recursive: true });
  const write = (path, text) => writeFileSync(join(repository, "docs", path), text);
  write("pages/home.astro", '<HomePage locale="en" />');
  write("pages/pt-BR/home.astro", '<HomePage locale="pt-BR" />');
  write("pages/platform.astro", "<main>Platform</main>");
  write("pages/adoption.astro", "<main>Live adoption data</main>");
  write("index.md", "# Documentation\n");
  write("components/HomePage.astro", "<main>Shared template</main>");
  write("components/site-frame-copy.ts", 'export const frameCopy = { en: { product: "Product" } };\n');
  const homeCopy = `export const homeCopy = {
  en: {
    title: "Agent memory",
  },
  "pt-BR": {
    title: "Memória para agentes",
  },
};
`;
  write("components/home-copy.ts", homeCopy);
  commit("2026-01-01T00:00:00Z");
  write("components/home-copy.ts", homeCopy.replace("Agent memory", "Shared agent memory"));
  commit("2026-01-02T00:00:00Z");

  const pages = {
    "/": "/docs/home",
    "/pt-BR": "/docs/pt-BR/home",
    "/platform": "/docs/platform",
    "/adoption": "/docs/adoption",
  };
  const initial = readSiteDates(repository, pages);
  assert.equal(initial.contentDates.get("docs/index.md"), "2026-01-01T00:00:00.000Z");
  assert.deepEqual(initial.productDates, {
    "/": "2026-01-02T00:00:00.000Z",
    "/pt-BR": "2026-01-01T00:00:00.000Z",
    "/platform": "2026-01-01T00:00:00.000Z",
  });

  write("components/HomePage.astro", "<main>Shared main content and a new link</main>");
  commit("2026-01-03T00:00:00Z");
  assert.deepEqual(readSiteDates(repository, pages).productDates, {
    "/platform": "2026-01-01T00:00:00.000Z",
  });
  write(
    "components/home-copy.ts",
    homeCopy
      .replace("Agent memory", "Shared agent memory")
      .replace("Memória para agentes", "Memória compartilhada para agentes"),
  );
  commit("2026-01-04T00:00:00Z");
  assert.deepEqual(readSiteDates(repository, pages).productDates, {
    "/pt-BR": "2026-01-04T00:00:00.000Z",
    "/platform": "2026-01-01T00:00:00.000Z",
  });
  write("components/site-frame-copy.ts", 'export const frameCopy = { en: { product: "Developer tools" } };\n');
  commit("2026-01-05T00:00:00Z");
  assert.deepEqual(readSiteDates(repository, pages).productDates, {});
});
