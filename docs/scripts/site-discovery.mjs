import { readFileSync, writeFileSync } from "node:fs";
import { escape } from "html-escaper";

const origin = "https://loom.clickhouse.com";

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
}
