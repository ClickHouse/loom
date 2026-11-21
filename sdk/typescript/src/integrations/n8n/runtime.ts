import { createRequire } from "node:module";

// n8n's ESM entry contains extensionless imports; use its CommonJS host entry.
const hostRequire = createRequire(typeof __filename === "string" ? __filename : import.meta.url);
export const { NodeConnectionTypes, NodeOperationError } = hostRequire("n8n-workflow") as typeof import("n8n-workflow");
