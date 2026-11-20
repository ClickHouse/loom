// Explicit fixture input for a host identity established before hook delivery.
import fs from "node:fs";
import path from "node:path";
import { bindProtectionAlias, createProtectionRoot, findProtectionAlias } from "../lib/session-protection.mjs";

export function establishFixtureSession(home, sessionId, host = "claude-code") {
  const base = path.join(home, ".loom", "session-protection", "v1");
  const existing = fs.existsSync(base) ? findProtectionAlias(host, sessionId, { base }) : undefined;
  if (existing) return existing;
  const root = createProtectionRoot({ base });
  bindProtectionAlias(root, host, sessionId);
  return root;
}
