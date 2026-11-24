import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { publishSiteDiscovery } from "./site-discovery.mjs";

const output = new URL("../.vercel/output/", import.meta.url);
const manifest = new URL("config.json", output);
const config = JSON.parse(readFileSync(manifest, "utf8"));
const publicPages = new Map([
  ["/", "/docs/home"],
  ["/platform", "/docs/platform"],
  ["/enterprise", "/docs/enterprise"],
  ["/telemetry", "/docs/telemetry"],
  ["/adoption", "/docs/adoption"],
]);
const staticRoutes = [...publicPages]
  .filter(([path]) => path !== "/adoption")
  .map(([path, dest]) => ({
    src: path === "/" ? "^/$" : `^${path}/?$`,
    dest,
  }));

if (!Array.isArray(config.routes)) throw new Error("Vercel build manifest has no routes.");
for (const { dest } of staticRoutes) {
  const file = new URL(`static${dest}/index.html`, output);
  if (!existsSync(file)) throw new Error(`Missing prerendered site page: ${fileURLToPath(file)}`);
}

// Vercel resolves the destination to a function or file without matching another route.
const adoption = config.routes.find(({ src, dest }) => src === "^/docs/adoption$" && dest === "_render");
if (!adoption || !existsSync(new URL(`functions/${adoption.dest}.func/`, output))) {
  throw new Error("Missing adoption render function.");
}
const siteRoutes = [{ src: "^/adoption/?$", dest: adoption.dest }, ...staticRoutes];

const publicPaths = new Set(siteRoutes.map(({ src }) => src));
config.routes = [...siteRoutes, ...config.routes.filter(({ src }) => !publicPaths.has(src))];
publishSiteDiscovery(new URL("static/", output), {
  root: [...publicPages.keys()],
  aliases: [...publicPages.values()],
});
writeFileSync(manifest, `${JSON.stringify(config, null, "\t")}\n`);
