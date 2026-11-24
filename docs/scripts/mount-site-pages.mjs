import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const output = new URL("../.vercel/output/", import.meta.url);
const manifest = new URL("config.json", output);
const config = JSON.parse(readFileSync(manifest, "utf8"));
const siteRoutes = [
  { src: "^/$", dest: "/docs/home" },
  { src: "^/adoption/?$", dest: "/docs/adoption" },
  { src: "^/telemetry/?$", dest: "/docs/telemetry" },
];

if (!Array.isArray(config.routes)) throw new Error("Vercel build manifest has no routes.");
for (const { dest } of siteRoutes) {
  if (config.routes.some((route) => route.src === `^${dest}$` && route.dest === "_render")) continue;
  const file = new URL(`static${dest}/index.html`, output);
  if (!existsSync(file)) throw new Error(`Missing prerendered site page: ${fileURLToPath(file)}`);
}

const publicPaths = new Set(siteRoutes.map(({ src }) => src));
config.routes = [...siteRoutes, ...config.routes.filter(({ src }) => !publicPaths.has(src))];
writeFileSync(manifest, `${JSON.stringify(config, null, "\t")}\n`);
