import { defineConfig } from "tsdown";

// ESM + CJS with their own declaration files. The package
// has no side effects and no Node-only imports: the dist tests check both.
export default defineConfig({
  entry: {
    index: "src/index.ts",
    "integrations/langchain": "src/integrations/langchain.ts",
    "integrations/mastra": "src/integrations/mastra.ts",
    "integrations/openai-agents": "src/integrations/openai-agents.ts",
    "integrations/strands": "src/integrations/strands.ts",
  },
  format: ["esm", "cjs"],
  fixedExtension: true,
  dts: true,
  sourcemap: true,
  platform: "neutral",
  target: "es2022",
  clean: true,
  outDir: "dist",
});
