import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const output = new URL("../.vercel/output/", import.meta.url);
const manifest = new URL("config.json", output);
const config = JSON.parse(readFileSync(manifest, "utf8"));
const staticRoutes = [
  { src: "^/$", dest: "/docs/home" },
  { src: "^/platform/?$", dest: "/docs/platform" },
  { src: "^/enterprise/?$", dest: "/docs/enterprise" },
  { src: "^/telemetry/?$", dest: "/docs/telemetry" },
];

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
writeFileSync(manifest, `${JSON.stringify(config, null, "\t")}\n`);
