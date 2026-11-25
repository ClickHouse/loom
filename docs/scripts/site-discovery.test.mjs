import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { publishSiteDiscovery } from "./site-discovery.mjs";

function publish(t, sitemap, unlistedPages = []) {
  const directory = mkdtempSync(join(tmpdir(), "loom-docs-discovery-"));
  t.after(() => rmSync(directory, { recursive: true }));
  mkdirSync(join(directory, "docs"));
  for (const page of unlistedPages) {
    const folder = join(directory, page.slice(1));
    mkdirSync(folder, { recursive: true });
    writeFileSync(join(folder, "index.html"), "<title>Published page</title>");
  }
  writeFileSync(join(directory, "docs/sitemap.xml"), sitemap);
  return publishSiteDiscovery(pathToFileURL(`${directory}/`), {
    root: ["/platform"],
    aliases: ["/docs/platform"],
  });
}

test("redirects missing translations and leaves translated and unknown paths alone", (t) => {
  const routes = publish(
    t,
    `<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>https://loom.clickhouse.com/docs</loc><xhtml:link hreflang="en" /><xhtml:link hreflang="x-default" /></url>
  <url><loc>https://loom.clickhouse.com/docs/es/use-cases</loc><xhtml:link hreflang="es" /></url>
  <url><loc>https://loom.clickhouse.com/docs/ja</loc><xhtml:link hreflang="ja" /></url>
  <url><loc>https://loom.clickhouse.com/docs/concepts/memory</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/ja/concepts/memory</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/api/v1.2</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/api/unlisted</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/platform</loc></url>
</urlset>`,
    ["/docs/es/api/unlisted"],
  );
  const redirect = (path) => {
    const route = routes.find(({ src }) => new RegExp(src).test(path));
    return route ? [route.status, path.replace(new RegExp(route.src), route.headers.Location)] : null;
  };

  assert.deepEqual(redirect("/docs/es"), [307, "/docs/es/use-cases"]);
  assert.deepEqual(redirect("/docs/es.mdx"), [307, "/docs/es/use-cases.mdx"]);
  assert.deepEqual(redirect("/docs/es/concepts/memory/"), [307, "/docs/concepts/memory"]);
  assert.deepEqual(redirect("/docs/es/concepts/memory.md"), [307, "/docs/concepts/memory.md"]);
  assert.deepEqual(redirect("/docs/es/api/v1.2.mdx"), [307, "/docs/api/v1.2.mdx"]);
  for (const path of [
    "/docs/es/use-cases",
    "/docs/ja",
    "/docs/ja/concepts/memory",
    "/docs/ja/concepts/memory.md",
    "/docs/en/concepts/memory",
    "/docs/es/api/unlisted",
    "/docs/es/api/unlisted.md",
    "/docs/es/api/v102",
    "/docs/es/platform",
    "/docs/es/unknown",
  ]) {
    assert.equal(redirect(path), null, path);
  }
});

test("splits route patterns as untranslated documentation grows", (t) => {
  const origin = "https://loom.clickhouse.com";
  const pages = Array.from({ length: 200 }, (_, index) => `operations/locale-integration-deployment-${index}`);
  const sitemap = [
    '<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    `<url><loc>${origin}/docs/es/use-cases</loc><xhtml:link hreflang="es" /></url>`,
    ...pages.map((path) => `<url><loc>${origin}/docs/${path}</loc></url>`),
    "</urlset>",
  ].join("\n");
  const routes = publish(t, sitemap);
  assert.ok(routes.every(({ src }) => src.length <= 4096));
  const patterns = routes.map(({ src }) => new RegExp(src));
  for (const path of pages) {
    assert.equal(patterns.filter((pattern) => pattern.test(`/docs/es/${path}`)).length, 1, path);
    assert.equal(patterns.filter((pattern) => pattern.test(`/docs/es/${path}.mdx`)).length, 1, path);
  }
});
