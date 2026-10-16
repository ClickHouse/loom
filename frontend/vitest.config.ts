import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Unit, component and axe tests. jsdom, one setup file, `src/**/*.test.{ts,tsx}` only —
// the Playwright smoke lives in `e2e/` and runs under `playwright.config.ts` against the
// production build.
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Specs import Testing Library through src/test/rtl.tsx, which adds the app's providers.
    alias: [
      { find: /^@testing-library\/react$/, replacement: fileURLToPath(new URL("./src/test/rtl.tsx", import.meta.url)) },
    ],
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    setupFiles: ["src/test/setup.ts"],
    css: false,
    // Click UI's ESM imports its CSS; inlined, Vite transforms those imports (and `css: false`
    // empties them) instead of Node failing on a `.css` file.
    server: { deps: { inline: [/@clickhouse\/click-ui/] } },
  },
});
