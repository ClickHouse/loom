import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { hostEnv } from "./host-env.mjs";

const plugin = fileURLToPath(new URL("..", import.meta.url));
const token = `a.${Buffer.from(JSON.stringify({ sub: "fixture", org: "fixture-org" })).toString("base64url")}.c`;
// Exercise real child entry points without a listener or external request.
const replies = String.raw`
import fs from "node:fs";
globalThis.fetch = async (input) => {
  fs.appendFileSync(process.env.FIXTURE_REQUESTS, String(input) + "\n");
  const url = new URL(String(input));
  if (url.origin !== "https://loom.example") throw new Error("Unexpected destination");
  const op = url.pathname.split("/").at(-1);
  const body = op === "health" ? {version: "1.2.0"}
    : op === "context.pack" ? {rendered: "fixture-primer"}
    : op === "namespace.list_mine" ? {rows: [{namespace: "work", role: "writer", status: "active"}]}
    : null;
  if (body === null) throw new Error("Unexpected operation");
  return new Response(JSON.stringify(body), {status: 200});
};
`;

for (const [name, invalid] of [
  ["origin-only endpoint", "https://loom.example/new"],
  ["duplicate query key", "https://loom.example/new/mcp?toolset=coding&toolset=all"],
]) {
  test(`standalone status refuses stale state after ${name}`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-endpoint-status-"));
    try {
      const bootstrap = path.join(dir, "replies.mjs");
      const requests = path.join(dir, "requests.log");
      fs.writeFileSync(bootstrap, replies);
      const env = hostEnv({
        PATH: process.env.PATH,
        HOME: dir,
        TMPDIR: dir,
        LOOM_CLIENT: "claude-code",
        LOOM_API_KEY: token,
        LOOM_NAMESPACE: "work",
        LOOM_AUTO_SIGNUP: "false",
        LOOM_CODE_MAP: "false",
        LOOM_TASK_INBOX_LINE: "false",
        LOOM_COLLECTIONS_LINE: "false",
        LOOM_SESSION_BRIEF_PRIMER: "false",
        LOOM_HOOK_EXIT_MS: "100",
        FIXTURE_REQUESTS: requests,
      });
      const run = (entry, extra = {}, args = []) => {
        const result = spawnSync(
          process.execPath,
          ["--import", pathToFileURL(bootstrap).href, path.join(plugin, entry), ...args],
          {
            input: JSON.stringify({ cwd: dir, session_id: "fixture-session" }),
            encoding: "utf8",
            timeout: 5000,
            env: { ...env, ...extra },
          },
        );
        assert.equal(result.error, undefined, String(result.error));
        return result;
      };
      const start = (url) =>
        run("hooks/loom-session-start.mjs", { CLAUDE_PLUGIN_OPTION_SERVER_URL: url }, ["SessionStart", "claude-code"]);

      const initial = start("https://loom.example/old/mcp?toolset=coding");
      assert.equal(initial.status, 0, initial.stdout + initial.stderr);
      const before = run("bin/loom-client-status.mjs");
      assert.equal(before.status, 0, before.stdout + before.stderr);
      assert.equal(JSON.parse(before.stdout).ok, true);

      fs.writeFileSync(requests, "");
      const rejected = start(invalid);
      assert.equal(rejected.status, 0, rejected.stderr);
      const after = run("bin/loom-client-status.mjs");
      assert.equal(after.status, 1, after.stdout + after.stderr);
      const report = JSON.parse(after.stdout);
      assert.equal(report.ok, false);
      assert.ok(report.checks.some((c) => c.grade === "FAIL" && c.detail.includes("configure server_url")));
      assert.equal(fs.readFileSync(requests, "utf8"), "");

      assert.equal(start("https://loom.example/new/mcp?toolset=coding").status, 0);
      fs.writeFileSync(requests, "");
      const repaired = run("bin/loom-client-status.mjs");
      assert.equal(repaired.status, 0, repaired.stdout + repaired.stderr);
      assert.equal(JSON.parse(repaired.stdout).ok, true);
      assert.ok(
        fs
          .readFileSync(requests, "utf8")
          .trim()
          .split("\n")
          .every((p) => new URL(p).pathname.startsWith("/new/")),
      );
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}
