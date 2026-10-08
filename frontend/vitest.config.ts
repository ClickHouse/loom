import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit, component and axe tests. jsdom, one setup file, `src/**/*.test.{ts,tsx}` only —
// the Playwright smoke lives in `e2e/` and runs under `playwright.config.ts` against the
// production build.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
