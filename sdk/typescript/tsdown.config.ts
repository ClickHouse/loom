import { defineConfig } from "tsdown";

// ESM + CJS with their own declaration files. The package
// has no side effects. The n8n subpath targets its Node.js host;
// the other bundles have no Node-only imports.
export default defineConfig({
  entry: {
    index: "src/index.ts",
    "integrations/ai-sdk": "src/integrations/ai-sdk.ts",
    "integrations/claude-memory": "src/integrations/claude-memory.ts",
    "integrations/google-adk": "src/integrations/google-adk.ts",
    "integrations/langchain": "src/integrations/langchain.ts",
    "integrations/mastra": "src/integrations/mastra.ts",
    "integrations/n8n": "src/integrations/n8n.ts",
    "integrations/openai-agents": "src/integrations/openai-agents.ts",
    "integrations/strands": "src/integrations/strands.ts",
  },
  format: ["esm", "cjs"],
  fixedExtension: true,
  dts: true,
  sourcemap: true,
  platform: "neutral",
  deps: { neverBundle: [/^node:/] },
  target: "es2022",
  clean: true,
  outDir: "dist",
});
