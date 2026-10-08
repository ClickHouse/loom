import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The two fonts every page renders with (Inter and JetBrains Mono, latin subset) are
// discovered only after the CSS has been downloaded and parsed, one round trip after the
// HTML. A preload link in the HTML starts them with the script. The other subsets stay
// lazy — `unicode-range` fetches them only for text that needs them.
const PRELOADED_FONTS = [/^inter-latin-wght-normal-/, /^jetbrains-mono-latin-wght-normal-/];

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
            attrs: { rel: "preload", as: "font", type: "font/woff2", crossorigin: "", href: `/me/${fileName}` },
            injectTo: "head" as const,
          });
        }
        return tags;
      },
    },
  };
}

// Build for /me sub-path so HTML and asset URLs are absolute under /me/.
// In dev (`npm run dev`), proxy API calls to the Loom server on :7777.
export default defineConfig({
  base: "/me/",
  plugins: [react(), tailwindcss(), fontPreload()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2020",
    // pdfjs (~1.2 MB worker) and mammoth (~500 KB) are already split
    // into their own chunks via dynamic `import()` from the upload
    // dialog — they only load when the user actually picks a binary
    // file. Bump the warning so the build output isn't drowned by
    // alarms about chunks that are deliberately fat-and-lazy.
    chunkSizeWarningLimit: 1500,
    // Vendors in their own long-lived chunks so an app change does not re-download React;
    // views split by React.lazy in App.tsx.
    rollupOptions: {
      output: {
        manualChunks: {
          // The vendor chunk carries what every page evaluates at boot. The dialog and
          // tabs primitives belong to the lazy dialogs, the inspector and a few views;
          // they ride with them (vendor 215 → 204 kB; the boot's module evaluation in
          // proportion).
          vendor: ["react", "react-dom", "zustand", "@radix-ui/react-popover", "@radix-ui/react-tooltip"],
          icons: ["lucide-react"],
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
