import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { publishSiteDiscovery } from "./site-discovery.mjs";

function staticFixture(t, sitemap, unlistedPages = []) {
  const directory = mkdtempSync(join(tmpdir(), "loom-docs-discovery-"));
  t.after(() => rmSync(directory, { recursive: true }));
  mkdirSync(join(directory, "docs"));
  for (const page of unlistedPages) {
    const folder = join(directory, page.slice(1));
    mkdirSync(folder, { recursive: true });
    writeFileSync(join(folder, "index.html"), "<title>Published page</title>");
  }
  writeFileSync(join(directory, "docs/sitemap.xml"), sitemap);
  return pathToFileURL(`${directory}/`);
}

function publish(t, sitemap, unlistedPages) {
  return publishSiteDiscovery(staticFixture(t, sitemap, unlistedPages), {
    root: ["/platform"],
    aliases: ["/docs/platform"],
  });
}

test("points published Markdown and MDX mirrors to their HTML canonicals", (t) => {
  const origin = "https://loom.clickhouse.com";
  const sitemap = `<urlset>
  <url><loc>${origin}/docs</loc></url>
  <url><loc>${origin}/docs/es/concepts/memory</loc></url>
  <url><loc>${origin}/docs/api/v1.2</loc></url>
  <url><loc>${origin}/docs/no-mirror</loc></url>
  <url><loc>${origin}/docs/platform</loc></url>
</urlset>`;
  const output = staticFixture(t, sitemap);
  for (const file of [
    "docs/index.md",
    "docs/index.mdx",
    "docs/es/concepts/memory.md",
    "docs/es/concepts/memory.mdx",
    "docs/api/v1.2.md",
    "docs/platform.md",
    "docs/404.md",
    "docs/llms-install.md",
  ]) {
    mkdirSync(new URL("./", new URL(file, output)), { recursive: true });
    writeFileSync(new URL(file, output), "# Page text");
  }
  const routes = publishSiteDiscovery(output, { root: ["/platform"], aliases: ["/docs/platform"] });
  const findCanonical = (path) => {
    const matching = routes.filter(({ src, headers }) => headers.Link && new RegExp(src).test(path));
    assert.ok(matching.length <= 1, path);
    const route = matching[0];
    if (!route) return undefined;
    assert.equal(route.continue, true);
    assert.equal(route.status, undefined);
    return path.replace(new RegExp(route.src), route.headers.Link);
  };

  for (const [path, canonical] of [
    ["/docs/index.md", "/docs"],
    ["/docs/index.mdx", "/docs"],
    ["/docs/es/concepts/memory.md", "/docs/es/concepts/memory"],
    ["/docs/es/concepts/memory.mdx", "/docs/es/concepts/memory"],
    ["/docs/api/v1.2.md", "/docs/api/v1.2"],
  ]) {
    assert.equal(findCanonical(path), `<${origin}${canonical}>; rel="canonical"`, path);
  }
  for (const path of [
    "/docs",
    "/docs/es/concepts/memory",
    "/docs/api/v1.2.mdx",
    "/docs/api/v102.md",
    "/docs/no-mirror.md",
    "/docs/platform.md",
    "/docs/404.md",
    "/docs/llms-install.md",
  ]) {
    assert.equal(findCanonical(path), undefined, path);
  }
});

test("publishes reciprocal homepage alternates only on canonical product homepages", (t) => {
  const origin = "https://loom.clickhouse.com";
  const sitemap = `<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>${origin}/docs</loc><lastmod>2026-01-01T00:00:00.000Z</lastmod></url>
  <url><loc>${origin}/docs/home</loc></url>
  <url><loc>${origin}/docs/pt-BR/home</loc></url>
</urlset>`;
  const output = staticFixture(t, sitemap);
  const homeLocales = { en: { href: "/" }, "pt-BR": { href: "/pt-BR" } };
  publishSiteDiscovery(output, {
    root: ["/", "/pt-BR", "/platform"],
    aliases: ["/docs/home", "/docs/pt-BR/home", "/docs/platform"],
    homeLocales,
    productDates: { "/": "2026-01-03T00:00:00.000Z", "/pt-BR": "2026-01-02T00:00:00.000Z" },
  });
  const publicSitemap = readFileSync(new URL("sitemap.xml", output), "utf8");
  const locations = new Map(
    [...publicSitemap.matchAll(/<url><loc>([^<]+)<\/loc>(.*?)<\/url>/g)].map(([, href, content]) => [href, content]),
  );
  const alternates = [
    ["en", origin + "/"],
    ["pt-BR", origin + "/pt-BR"],
    ["x-default", origin + "/"],
  ];
  for (const path of ["/", "/pt-BR"]) {
    const content = locations.get(origin + path);
    assert.deepEqual(
      [...content.matchAll(/hreflang="([^"]+)" href="([^"]+)"/g)].map(([, lang, href]) => [lang, href]),
      alternates,
    );
  }
  assert.equal(locations.get(origin + "/platform"), "");
  assert.match(locations.get(origin + "/"), /^<lastmod>2026-01-03T00:00:00.000Z<\/lastmod>/);
  assert.match(locations.get(origin + "/pt-BR"), /^<lastmod>2026-01-02T00:00:00.000Z<\/lastmod>/);
  assert.equal(locations.get(origin + "/docs"), "<lastmod>2026-01-01T00:00:00.000Z</lastmod>");
  assert.ok(!publicSitemap.includes("/docs/pt-BR/home"));
  const docsSitemap = readFileSync(new URL("docs/sitemap.xml", output), "utf8");
  assert.ok(!docsSitemap.includes("/pt-BR"));
});

test("keeps localized product-page alternates on their own canonical routes", (t) => {
  const origin = "https://loom.clickhouse.com";
  const sitemap = `<urlset xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>${origin}/docs</loc></url>
  <url><loc>${origin}/docs/de/enterprise</loc></url>
  <url><loc>${origin}/docs/fr/enterprise</loc></url>
  <url><loc>${origin}/docs/es/knowledge-factory</loc></url>
  <url><loc>${origin}/docs/use-cases/knowledge-factory</loc></url>
</urlset>`;
  const output = staticFixture(t, sitemap);
  const enterprisePages = ["/enterprise", "/de/enterprise", "/fr/enterprise"];
  const factoryPages = ["/knowledge-factory", "/es/knowledge-factory"];
  const productPages = [...enterprisePages, ...factoryPages];
  publishSiteDiscovery(output, {
    root: ["/", "/de", "/es", "/fr", ...productPages],
    aliases: productPages.map((path) => `/docs${path}`),
    homeLocales: {
      en: { href: "/", enterprise: "/enterprise", knowledgeFactory: "/knowledge-factory" },
      de: { href: "/de", enterprise: "/de/enterprise" },
      es: { href: "/es", knowledgeFactory: "/es/knowledge-factory" },
      fr: { href: "/fr", enterprise: "/fr/enterprise" },
    },
  });
  const publicSitemap = readFileSync(new URL("sitemap.xml", output), "utf8");
  const locations = new Map(
    [...publicSitemap.matchAll(/<url><loc>([^<]+)<\/loc>(.*?)<\/url>/g)].map(([, href, content]) => [href, content]),
  );
  for (const [pages, languages] of [
    [enterprisePages, ["en", "de", "fr"]],
    [factoryPages, ["en", "es"]],
  ]) {
    const expected = pages.map((path, index) => [languages[index], origin + path]);
    expected.push(["x-default", origin + pages[0]]);
    for (const path of pages) {
      assert.deepEqual(
        [...locations.get(origin + path).matchAll(/hreflang="([^"]+)" href="([^"]+)"/g)].map(([, language, href]) => [
          language,
          href,
        ]),
        expected,
      );
      assert.ok(!publicSitemap.includes(`/docs${path}`));
    }
  }
  assert.ok(!locations.get(origin + "/fr").includes("/enterprise"));
  assert.ok(!locations.get(origin + "/es").includes("/knowledge-factory"));
  assert.equal(locations.get(origin + "/docs/use-cases/knowledge-factory"), "");
});

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
