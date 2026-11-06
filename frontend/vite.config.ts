import { defineConfig, type Plugin, type Rollup } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

// The two fonts every page renders with (Inter and Inconsolata, latin subset) are
// discovered only after the CSS has been downloaded and parsed, one round trip after the
// HTML. A preload link in the HTML starts them with the script. The other subsets stay
// lazy — `unicode-range` fetches them only for text that needs them.
const PRELOADED_FONTS = [/^inter-latin-wght-normal-/, /^inconsolata-latin-wght-normal-/];

function fontPreload(): Plugin {
  return {
    name: "loom-font-preload",
    apply: "build",
    transformIndexHtml: {
      order: "post",
      handler(_html, ctx) {
        const bundle = ctx.bundle ?? {};
        const tags = [];
        for (const fileName of Object.keys(bundle)) {
          const base = fileName.split("/").pop() ?? "";
          if (!base.endsWith(".woff2") || !PRELOADED_FONTS.some((re) => re.test(base))) continue;
          tags.push({
            tag: "link",
            attrs: { rel: "preload", as: "font", type: "font/woff2", crossorigin: "", href: `/${fileName}` },
            injectTo: "head" as const,
          });
        }
        return tags;
      },
    },
  };
}

// Whether a module is reached from the entry by static imports alone, so the first paint
// waits on it.
function loadsAtBoot(id: string, getModuleInfo: Rollup.GetModuleInfo, seen = new Set<string>()): boolean {
  const info = getModuleInfo(id);
  if (!info || seen.has(id)) return false;
  if (info.isEntry) return true;
  seen.add(id);
  return info.importers.some((importer) => loadsAtBoot(importer, getModuleInfo, seen));
}

// Served at the server root, so HTML and asset URLs are absolute under /.
// In dev (`npm run dev`), proxy API calls to the Loom server on :7777.
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss(), fontPreload()],
  resolve: {
    alias: [
      // Click UI's dark tokens and page defaults, which its exports map does not expose.
      // Loaded directly instead of through ClickUIProvider, whose barrel import would ship
      // every component's CSS and both themes.
      {
        find: "click-ui-styles",
        replacement: fileURLToPath(
          new URL("./node_modules/@clickhouse/click-ui/dist/esm/theme/styles", import.meta.url),
        ),
      },
      // Click UI's Icon resolves a name against its flag, logo and payment-mark maps too,
      // importing them whole (~1 MB); the dashboard draws only interface icons.
      {
        find: /^.*\/(Flags|Logos|Payments)(Light|Dark)\.js$/,
        replacement: fileURLToPath(new URL("./src/lib/emptyAssetMap.ts", import.meta.url)),
      },
    ],
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
    // Views split by React.lazy in App.tsx.
    rollupOptions: {
      output: {
        // A shared module only lazy views use (fieldError, Stat, TabTrigger, ...) would be a
        // chunk of a few hundred bytes, one request each; Rollup merges a chunk under 1 kB
        // into one that loads with it.
        experimentalMinChunkSize: 1000,
        // Long-lived vendor chunks, so an app change does not re-download them. A function, not
        // a package list: a listed package brings its whole entry, and Click UI's entry
        // re-exports every component and its CSS. Only the modules the app imports go in, and
        // of Click UI only those the shell loads at boot; the components only lazy views use
        // (Select and its sortable list, Table, Pagination, ...) split with those views.
        manualChunks(id, { getModuleInfo }) {
          if (id.includes("/node_modules/@clickhouse/click-ui/") && loadsAtBoot(id, getModuleInfo)) return "clickui";
          if (/\/node_modules\/(react|react-dom|scheduler|zustand|@radix-ui\/[^/]+)\//.test(id)) return "vendor";
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://127.0.0.1:7777",
      "/mcp": "http://127.0.0.1:7777",
    },
  },
});
