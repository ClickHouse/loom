// The SessionStart primer says when the MCP header helper will send a
// config token saved for another server than Claude Code's MCP entry calls:
// in Claude Code only, and never for an agent, whose tools send its own token.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HOOK = fileURLToPath(new URL("../hooks/loom-session-start.mjs", import.meta.url));
const SAVED_FOR = "https://a.example";

async function server() {
  const srv = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        req.url.startsWith("/oauth/token") ? JSON.stringify({ access_token: "agent.tok", expires_in: 3600 }) : "{}",
      );
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { srv, url: `http://127.0.0.1:${srv.address().port}` };
}

function run(url, { args = [], env = {} } = {}) {
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-notice-")));
  fs.mkdirSync(path.join(home, ".loom"), { recursive: true });
  fs.writeFileSync(path.join(home, ".loom", "config.json"), JSON.stringify({ token: "cfg.tok", url: SAVED_FOR }), {
    mode: 0o600,
  });
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, ...args], {
      cwd: home,
      env: { PATH: process.env.PATH, HOME: home, TMPDIR: home, LOOM_BASE_URL: url, LOOM_CODE_MAP: "false", ...env },
    });
    let stdout = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.on("close", () => resolve(stdout));
    child.stdin.end("{}");
  });
}

test("the primer names the mismatch in Claude Code, and not for Codex or an agent", async () => {
  const { srv, url } = await server();
  try {
    const claude = await run(url);
    assert.match(
      claude,
      new RegExp(`holds a token saved for ${SAVED_FOR}, which the loom MCP tools send to ${url}\\.`),
    );
    assert.equal(claude.includes("cfg.tok"), false);
    const codex = await run(url, { args: ["SessionStart", "codex"] });
    assert.ok(codex.length > 0, "the Codex primer still renders");
    assert.equal(codex.includes("holds a token saved for"), false);
    const agent = await run(url, { env: { LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "proof" } });
    assert.ok(agent.length > 0, "the agent primer still renders");
    assert.equal(agent.includes("holds a token saved for"), false);
  } finally {
    srv.close();
  }
});
