/**
 * The built package is the contract: both bundles load, export
 * exactly the documented surface, agree on the version with package.json and
 * pull in no Node built-ins (the SDK runs on fetch alone).
 */

import { readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "..", "..");
const dist = join(root, "dist");
const require = createRequire(import.meta.url);
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8")) as {
  version: string;
  exports: Record<string, { import: { types: string; default: string }; require: { types: string; default: string } }>;
  sideEffects: boolean;
};

/** Every runtime export of `@clickhouse/loom`. Adding one is a deliberate API change: update this list. */
const ROOT_EXPORTS = [
  "Agent",
  "AgentTools",
  "AuthError",
  "BackendUnavailable",
  "Capabilities",
  "DEFAULT_LOG_LEVEL",
  "DEFAULT_RETRY_AFTER_S",
  "DEFAULT_RETRY_POLICY",
  "Datapack",
  "GENERATED_OP_COUNT",
  "INGEST_DONE",
  "INVALID_RESPONSE_CODE",
  "IdempotentConflict",
  "IdempotentInProgress",
  "LOG_LEVELS",
  "LOOM_TOOL_SPECS",
  "LoomClient",
  "LoomError",
  "MAX_RETRY_AFTER_S",
  "MachineLogin",
  "NETWORK_ERROR_CODE",
  "NOOP_LOGGER",
  "NotFound",
  "OPS",
  "OP_BY_NAME",
  "PreconditionFailed",
  "ROLE_RANK",
  "RateLimited",
  "SDK_IDENT",
  "SDK_NAME",
  "SDK_VERSION",
  "SENTINEL",
  "Session",
  "StoredLogin",
  "TimeoutError",
  "Transport",
  "ValidationError",
  "accessToken",
  "backoffDelayMs",
  "capabilities",
  "collect",
  "consoleLogger",
  "decodeTokenClaims",
  "defaultProfile",
  "errorFromResponse",
  "login",
  "logout",
  "machineLogin",
  "paginate",
  "parseLogLevel",
  "parseRetryAfter",
  "probe",
  "randomId",
  "readEnv",
  "redactHeaderValue",
  "redactHeaders",
  "resolveRetryPolicy",
  "retryAfterFrom",
  "roleOn",
  "roleSatisfies",
  "runtimeIdent",
  "sameServer",
  "toolDefinitions",
  "whoami",
  "withLevel",
];

function names(mod: object): string[] {
  return Object.keys(mod)
    .filter((k) => k !== "default" && k !== "__esModule")
    .sort();
}

describe("dist", () => {
  it("package.json declares ESM + CJS conditions with types for every subpath, and no side effects", () => {
    expect(pkg.sideEffects).toBe(false);
    for (const [sub, cond] of Object.entries(pkg.exports)) {
      if (sub === "./package.json") continue;
      for (const c of [cond.import, cond.require]) {
        expect(c.types, `${sub} types`).toMatch(/\.d\.(m|c)ts$/);
        expect(() => readFileSync(join(root, c.types))).not.toThrow();
        expect(() => readFileSync(join(root, c.default))).not.toThrow();
      }
    }
  });

  it("the ESM bundle exports exactly the documented surface", async () => {
    const esm = (await import(join(dist, "index.mjs"))) as object;
    expect(names(esm)).toEqual(ROOT_EXPORTS);
  });

  it("the CJS bundle exports the same surface and the same version", async () => {
    const cjs = require(join(dist, "index.cjs")) as Record<string, unknown>;
    expect(names(cjs)).toEqual(ROOT_EXPORTS);
    const esm = (await import(join(dist, "index.mjs"))) as { SDK_VERSION: string; GENERATED_OP_COUNT: number };
    expect(cjs.SDK_VERSION).toBe(pkg.version);
    expect(esm.SDK_VERSION).toBe(pkg.version);
    expect(cjs.GENERATED_OP_COUNT).toBe(esm.GENERATED_OP_COUNT);
  });

  it("the strands subpath loads in both formats", async () => {
    const esm = (await import(join(dist, "integrations/strands.mjs"))) as object;
    const cjs = require(join(dist, "integrations/strands.cjs")) as object;
    expect(names(esm)).toEqual(names(cjs));
    expect(names(esm)).toContain("LoomMemoryStore");
  });

  it("no bundle imports a Node built-in", () => {
    const files = readdirSync(dist, { recursive: true }) as string[];
    for (const f of files.filter((n) => /\.(mjs|cjs)$/.test(n))) {
      const text = readFileSync(join(dist, f), "utf8");
      expect(text, f).not.toMatch(/(require\(|from\s*)["'](node:|fs|path|crypto|http|https|util|os)["']/);
    }
  });
});
