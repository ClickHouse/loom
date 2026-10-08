import { defineConfig } from "vitest/config";

// Runs against a live Loom: LOOM_BASE_URL + LOOM_API_KEY (+ LOOM_ORG, LOOM_NAMESPACE).
export default defineConfig({
  test: {
    include: ["test/integ/**/*.test.ts"],
    environment: "node",
    testTimeout: 120_000,
  },
});
