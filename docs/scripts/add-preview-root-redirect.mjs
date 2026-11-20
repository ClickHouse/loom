import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const docs = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = join(docs, ".vercel", "output", "config.json");
const config = JSON.parse(readFileSync(manifest, "utf8"));

if (!Array.isArray(config.routes)) throw new Error("Vercel build manifest has no routes.");

config.routes = [
  {
    src: "^/$",
    headers: { Location: "/docs" },
    status: 307,
  },
  ...config.routes.filter((route) => route.src !== "^/$"),
];

writeFileSync(manifest, `${JSON.stringify(config, null, "\t")}\n`);
