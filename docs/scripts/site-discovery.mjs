import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { escape } from "html-escaper";

const origin = "https://loom.clickhouse.com";
const maxRoutePatternLength = 4096;

function escapePattern(path) {
  return path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function splitAlternatives(paths, limit) {
  const groups = [];
  let group = "";
  for (const path of paths) {
    if (group && group.length + path.length + 1 > limit) {
      groups.push(group);
      group = "";
    }
    group = group ? `${group}|${path}` : path;
  }
  if (group) groups.push(group);
  return groups;
}

function translationRedirects(sitemap, staticRoot) {
  const paths = new Set([...sitemap.matchAll(/<loc>([^<]+)<\/loc>/g)].map(([, url]) => new URL(url).pathname));
  const locales = new Set([...sitemap.matchAll(/hreflang="([^"]+)"/g)].map(([, locale]) => locale));
  locales.delete("en");
  locales.delete("x-default");

  const englishPaths = [...paths]
    .filter((path) => path.startsWith("/docs/") && !locales.has(path.split("/")[2]))
    .map((path) => path.slice("/docs/".length));
  const hasPage = (path) => paths.has(path) || existsSync(new URL(`${path.slice(1)}/index.html`, staticRoot));
  const redirect = (src, location) => ({ src, status: 307, headers: { Location: location } });

  return [...locales].flatMap((locale) => {
    const base = `/docs/${locale}`;
    const pattern = escapePattern(base);
    const redirects = [];
    if (!hasPage(base)) {
      const localIndex = `${base}/use-cases`;
      const landing = hasPage(localIndex) ? localIndex : "/docs";
      const landingExport = landing === "/docs" ? "/docs/index" : landing;
      redirects.push(redirect(`^${pattern}/?$`, landing), redirect(`^${pattern}(\\.mdx?)$`, `${landingExport}$1`));
    }

    const missing = englishPaths.filter((path) => !hasPage(`${base}/${path}`)).map(escapePattern);
    const prefix = `^${pattern}/(`;
    const exportSuffix = ")(\\.mdx?)$";
    const limit = maxRoutePatternLength - prefix.length - exportSuffix.length;
    for (const alternatives of splitAlternatives(missing, limit)) {
      const source = `${prefix}${alternatives}`;
      redirects.push(redirect(`${source})/?$`, "/docs/$1"), redirect(`${source}${exportSuffix}`, "/docs/$1$2"));
    }
    return redirects;
  });
}

export function publishSiteDiscovery(staticRoot, { root, aliases }) {
  const docsSitemap = new URL("docs/sitemap.xml", staticRoot);
  const sitemap = readFileSync(docsSitemap, "utf8");
  if (!sitemap.includes("</urlset>")) {
    throw new Error("Expected a docs sitemap URL set.");
  }

  const aliasLocations = new Set(aliases.map((path) => escape(new URL(path, origin).href)));
  const docs = sitemap.replace(/^[\t ]*<url>[\s\S]*?<\/url>\n?/gm, (entry) => {
    const location = entry.match(/<loc>([^<]+)<\/loc>/)?.[1];
    return aliasLocations.has(location) ? "" : entry;
  });
  const siteEntries = root.map((path) => `  <url><loc>${escape(new URL(path, origin).href)}</loc></url>`);

  writeFileSync(docsSitemap, docs);
  writeFileSync(new URL("sitemap.xml", staticRoot), docs.replace("</urlset>", `${siteEntries.join("\n")}\n</urlset>`));
  writeFileSync(new URL("robots.txt", staticRoot), `User-agent: *\nAllow: /\n\nSitemap: ${origin}/sitemap.xml\n`);
  return translationRedirects(docs, staticRoot);
}
