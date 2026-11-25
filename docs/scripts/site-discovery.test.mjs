import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { publishSiteDiscovery } from "./site-discovery.mjs";

test("redirects missing translations and leaves translated and unknown paths alone", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "loom-docs-discovery-"));
  t.after(() => rmSync(directory, { recursive: true }));
  mkdirSync(join(directory, "docs"));
  writeFileSync(
    join(directory, "docs/sitemap.xml"),
    `<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>https://loom.clickhouse.com/docs</loc><xhtml:link hreflang="en" /><xhtml:link hreflang="x-default" /></url>
  <url><loc>https://loom.clickhouse.com/docs/es/use-cases</loc><xhtml:link hreflang="es" /></url>
  <url><loc>https://loom.clickhouse.com/docs/ja</loc><xhtml:link hreflang="ja" /></url>
  <url><loc>https://loom.clickhouse.com/docs/concepts/memory</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/ja/concepts/memory</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/api/v1.2</loc></url>
  <url><loc>https://loom.clickhouse.com/docs/platform</loc></url>
</urlset>`,
  );
  const routes = publishSiteDiscovery(pathToFileURL(`${directory}/`), {
    root: ["/platform"],
    aliases: ["/docs/platform"],
  });
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
    "/docs/es/api/v102",
    "/docs/es/platform",
    "/docs/es/unknown",
  ]) {
    assert.equal(redirect(path), null, path);
  }
});
