import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { escape } from "html-escaper";

const origin = "https://loom.clickhouse.com";
const daumOwnershipProof =
  "#DaumWebMasterTool:a363b3401eee3c430ffb111a936fcad7fe4ff06819aca82edabb1196d703baab:O++XJBLIZbSwCRTbPGXamQ==";
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

function markdownCanonicalRoutes(paths, staticRoot) {
  const canonical = (src, path) => ({
    src,
    headers: { Link: `<${origin}${path}>; rel="canonical"` },
    continue: true,
    caseSensitive: true,
  });

  return ["md", "mdx"].flatMap((extension) => {
    const hasMirror = (path) => existsSync(new URL(`${path.slice(1)}.${extension}`, staticRoot));
    const pages = [...paths].filter((path) => path.startsWith("/docs/") && hasMirror(path)).map(escapePattern);
    const prefix = "^(";
    const suffix = `)\\.${extension}$`;
    const limit = maxRoutePatternLength - prefix.length - suffix.length;
    const routes = splitAlternatives(pages, limit).map((group) => canonical(`${prefix}${group}${suffix}`, "$1"));
    if (paths.has("/docs") && hasMirror("/docs/index")) {
      routes.push(canonical(`^/docs/index\\.${extension}$`, "/docs"));
    }
    return routes;
  });
}

function translationRedirects(sitemap, paths, staticRoot) {
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

export function publishSiteDiscovery(staticRoot, { root, aliases, homeLocales = {}, productDates = {} }) {
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
  const alternatesByPath = new Map();
  for (const field of ["href", "enterprise", "knowledgeFactory"]) {
    const localized = Object.entries(homeLocales).flatMap(([language, routes]) =>
      routes[field] ? [[language, routes[field]]] : [],
    );
    if (localized.length === 0) continue;
    const defaultPath = homeLocales.en?.[field];
    if (!defaultPath || localized.some(([, href]) => !root.includes(href))) {
      throw new Error(`Localized routes for ${field} must include English and point to mounted public pages.`);
    }
    const links = [...localized, ["x-default", defaultPath]]
      .map(
        ([language, href]) =>
          `<xhtml:link rel="alternate" hreflang="${escape(language)}" href="${escape(new URL(href, origin).href)}" />`,
      )
      .join("");
    for (const [, href] of localized) alternatesByPath.set(href, links);
  }
  const siteEntries = root.map((path) => {
    const loc = escape(new URL(path, origin).href);
    const lastmod = productDates[path] ? `<lastmod>${escape(productDates[path])}</lastmod>` : "";
    return `  <url><loc>${loc}</loc>${lastmod}${alternatesByPath.get(path) ?? ""}</url>`;
  });

  writeFileSync(docsSitemap, docs);
  writeFileSync(new URL("sitemap.xml", staticRoot), docs.replace("</urlset>", `${siteEntries.join("\n")}\n</urlset>`));
  writeFileSync(
    new URL("robots.txt", staticRoot),
    `User-agent: *\nAllow: /\n\nSitemap: ${origin}/sitemap.xml\n\n${daumOwnershipProof}\n`,
  );
  const paths = new Set([...docs.matchAll(/<loc>([^<]+)<\/loc>/g)].map(([, url]) => new URL(url).pathname));
  return [...translationRedirects(docs, paths, staticRoot), ...markdownCanonicalRoutes(paths, staticRoot)];
}
