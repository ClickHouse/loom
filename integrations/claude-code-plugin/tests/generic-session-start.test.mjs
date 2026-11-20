import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { hostEnv } from "./host-env.mjs";

// The real hook runs against in-process replies: no listener or network access.
const replies = String.raw`
import fs from "node:fs";
globalThis.fetch = async (input) => {
  fs.appendFileSync(process.env.FIXTURE_REQUESTS, String(input) + "\n");
  const url = new URL(String(input));
  if (url.origin !== "https://loom.example") throw new Error("Unexpected destination");
  const op = url.pathname.split("/").at(-1);
  const body = op === "health" ? {version: "1.2.0"}
    : op === "context.pack" ? {rendered: "fixture-primer"}
    : op === "collection.list" ? {collections: [{namespace: "col-guide", role: "writer"}]}
    : op === "namespace.get" ? {record_conversations: false}
    : null;
  if (body === null) throw new Error("Unexpected operation");
  return new Response(JSON.stringify(body), {status: 200});
};
`;

for (const [name, plugin, client, overrides, accepts] of [
  [
    "generic origin option",
    "claude-code-plugin",
    "cursor",
    { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example" },
    true,
  ],
  [
    "generic proxy option",
    "claude-code-plugin",
    "cursor",
    { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example/proxy" },
    true,
  ],
  [
    "fixed entry origin option",
    "codex-plugin",
    "codex",
    { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example" },
    true,
  ],
  [
    "generic option credential",
    "claude-code-plugin",
    "cursor",
    {
      CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example",
      CLAUDE_PLUGIN_OPTION_TOKEN: "fixture-token",
      LOOM_API_KEY: "",
    },
    true,
  ],
  ["generic base environment", "claude-code-plugin", "cursor", { LOOM_BASE_URL: "https://loom.example" }, true],
  [
    "complete Claude endpoint",
    "claude-code-plugin",
    "claude-code",
    { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example/mcp?toolset=coding" },
    true,
  ],
  [
    "invalid Claude endpoint",
    "claude-code-plugin",
    "claude-code",
    { CLAUDE_PLUGIN_OPTION_SERVER_URL: "https://loom.example" },
    false,
  ],
]) {
  test(`SessionStart preserves the client URL contract: ${name}`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-generic-start-"));
    try {
      const bootstrap = path.join(dir, "replies.mjs");
      const requests = path.join(dir, "requests.log");
      fs.writeFileSync(bootstrap, replies);
      const hook = fileURLToPath(new URL(`../../${plugin}/hooks/loom-session-start.mjs`, import.meta.url));
      const result = spawnSync(
        process.execPath,
        ["--import", pathToFileURL(bootstrap).href, hook, "SessionStart", client],
        {
          input: JSON.stringify({ cwd: dir, session_id: "fixture-session" }),
          encoding: "utf8",
          timeout: 5000,
          env: hostEnv({
            PATH: process.env.PATH,
            HOME: dir,
            TMPDIR: dir,
            LOOM_API_KEY: "fixture-token",
            LOOM_AUTO_SIGNUP: "false",
            LOOM_CODE_MAP: "false",
            LOOM_TASK_INBOX_LINE: "false",
            LOOM_COLLECTIONS_LINE: "true",
            LOOM_SESSION_BRIEF_PRIMER: "false",
            LOOM_HOOK_EXIT_MS: "100",
            FIXTURE_REQUESTS: requests,
            ...overrides,
          }),
        },
      );
      assert.equal(result.error, undefined, String(result.error));
      assert.equal(result.status, 0, result.stderr);
      const calls = fs.existsSync(requests) ? fs.readFileSync(requests, "utf8").trim().split("\n") : [];
      if (!accepts) {
        assert.equal(calls.length, 0);
        assert.equal(result.stdout.includes("fixture-primer"), false);
        return;
      }
      assert.ok(result.stdout.includes("fixture-primer"), result.stdout + result.stderr);
      assert.ok(result.stdout.includes("col-guide"), result.stdout + result.stderr);
      assert.ok(
        calls.some((url) => url.endsWith("/v1/context.pack")),
        calls.join("\n"),
      );
      assert.ok(
        calls.some((url) => url.endsWith("/v1/collection.list")),
        calls.join("\n"),
      );
      if (overrides.CLAUDE_PLUGIN_OPTION_SERVER_URL?.endsWith("/proxy")) {
        assert.ok(
          calls.every((url) => new URL(url).pathname.startsWith("/proxy/")),
          calls.join("\n"),
        );
      }
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}
