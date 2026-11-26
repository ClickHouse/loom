import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { publishSiteDiscovery } from "./site-discovery.mjs";

const output = new URL("../.vercel/output/", import.meta.url);
const docs = new URL("../", import.meta.url);
const manifest = new URL("config.json", output);
const config = JSON.parse(readFileSync(manifest, "utf8"));
const publicPagesFile = new URL("./public-pages.json", import.meta.url);
const publicPages = new Map(Object.entries(JSON.parse(readFileSync(publicPagesFile, "utf8"))));
const homeLocales = JSON.parse(readFileSync(new URL("./home-locales.json", import.meta.url), "utf8"));
const productDates = JSON.parse(readFileSync(new URL(".blume-content/.product-dates.json", docs), "utf8"));
const adoptionDestination = "/docs/adoption";
const snapshotPath = "/api/oss-snapshot";
const telemetryFunctions = [
  ["/api/v1/adoption", "adoption-collector.mjs"],
  [snapshotPath, "oss-snapshot.mjs"],
];

if (!Array.isArray(config.routes)) throw new Error("Vercel build manifest has no routes.");
const siteRoutes = [...publicPages].map(([path, destination]) => {
  const src = path === "/" ? "^/$" : `^${path}/?$`;
  if (destination === adoptionDestination) {
    // Vercel resolves the destination without matching another route.
    const adoption = config.routes.find(({ src, dest }) => src === `^${adoptionDestination}$` && dest === "_render");
    if (!adoption || !existsSync(new URL(`functions/${adoption.dest}.func/`, output))) {
      throw new Error("Missing adoption render function.");
    }
    return { src, dest: adoption.dest };
  }
  const file = new URL(`static${destination}/index.html`, output);
  if (!existsSync(file)) throw new Error(`Missing prerendered site page: ${fileURLToPath(file)}`);
  return { src, dest: destination };
});

const telemetryRoutes = telemetryFunctions.map(([path, handler]) => {
  const directory = new URL(`functions${path}.func/`, output);
  mkdirSync(directory, { recursive: true });
  copyFileSync(new URL(`lib/${handler}`, docs), new URL(handler, directory));
  const functionConfig = {
    runtime: "nodejs24.x",
    handler,
    launcherType: "Nodejs",
    maxDuration: 10,
    supportsResponseStreaming: true,
  };
  writeFileSync(new URL(".vc-config.json", directory), JSON.stringify(functionConfig));
  return { src: `^${path}$`, dest: path };
});

const agentIndexRedirects = ["", "-full"].map((suffix) => ({
  src: `^/llms${suffix}\\.txt$`,
  status: 308,
  headers: { Location: `/docs/llms${suffix}.txt` },
}));
const aliasRedirects = [...publicPages].map(([path, destination]) => ({
  src: `^${destination}/?$`,
  status: 308,
  headers: { Location: path },
}));
const translationRedirects = publishSiteDiscovery(new URL("static/", output), {
  root: [...publicPages.keys()],
  aliases: [...publicPages.values()],
  homeLocales,
  productDates,
});
const publicPaths = new Set(siteRoutes.map(({ src }) => src));
config.routes = [
  ...agentIndexRedirects,
  ...aliasRedirects,
  ...translationRedirects,
  ...telemetryRoutes,
  ...siteRoutes,
  ...config.routes.filter(({ src }) => !publicPaths.has(src)),
];
config.crons = [...(config.crons ?? []), { path: snapshotPath, schedule: "30 2 * * *" }];
copyFileSync(new URL("../llms-install.md", docs), new URL("static/docs/llms-install.md", output));

if (process.env.VERCEL_TARGET_ENV === "production") {
  const revision = process.env.VERCEL_GIT_COMMIT_SHA;
  if (!revision || !/^[0-9a-f]{40}$/.test(revision)) throw new Error("Invalid production revision.");
  const wellKnown = new URL("static/.well-known/", output);
  mkdirSync(wellKnown, { recursive: true });
  writeFileSync(new URL("loom-revision.txt", wellKnown), `${revision}\n`);
  config.routes.unshift({
    src: "^/\\.well-known/loom-revision\\.txt$",
    headers: { "Cache-Control": "no-store", "X-Robots-Tag": "noindex" },
    continue: true,
  });
}
writeFileSync(manifest, `${JSON.stringify(config, null, "\t")}\n`);

for (const file of readdirSync(docs).filter((name) => /^[0-9a-f]{32}\.txt$/.test(name))) {
  copyFileSync(new URL(file, docs), new URL(`static/${file}`, output));
}
