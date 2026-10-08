import { defineConfig } from "vitest/config";

// Runs against the built package (`npm run build` first): loads the ESM and CJS
// bundles and checks the exported surface, the version and the absence of
// Node-only imports.
export default defineConfig({
  test: {
    include: ["test/package/**/*.test.ts"],
    environment: "node",
  },
});
