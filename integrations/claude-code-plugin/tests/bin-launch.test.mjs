// The plugin's bin/ entrypoints launched the way a skill or the host runs
// them: missing token, unreachable server, a healthy server.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const bin = (name) => fileURLToPath(new URL(`../bin/${name}`, import.meta.url));
const TOKEN = [
  Buffer.from('{"alg":"none"}').toString("base64url"),
  Buffer.from(JSON.stringify({ org: "o", namespaces: ["ns"], sub: "t", exp: 4102444800 })).toString("base64url"),
  "sig-not-printed",
].join(".");

function launch(name, args, env, stdin = "") {
  return new Promise((resolve) => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-bin-"));
    const child = spawn(process.execPath, [bin(name), ...args], {
      cwd: home,
      env: { PATH: process.env.PATH, HOME: home, TMPDIR: home, LOOM_AUTO_SIGNUP: "off", ...env },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    child.stdin.end(stdin);
  });
}

async function fakeLoom() {
  const srv = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(200, { "content-type": "application/json" });
      if (req.url === "/v1/health") return res.end(JSON.stringify({ build_commit: "abc" }));
      if (req.url === "/v1/namespace.list_mine")
        return res.end(JSON.stringify({ rows: [{ namespace: "ns", role: "writer", status: "active" }] }));
      if (req.url === "/v1/session.brief_list")
        return res.end(JSON.stringify({ briefs: [{ brief_key: "auto/p/main", revision: 2, age_seconds: 60 }] }));
      res.end("{}");
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${srv.address().port}`, close: () => new Promise((r) => srv.close(r)) };
}

const UNREACHABLE = "http://127.0.0.1:9";

test("loom-brief: no token, no server, a healthy server", async () => {
  const none = await launch("loom-brief.mjs", ["list"], { LOOM_BASE_URL: UNREACHABLE });
  assert.equal(none.code, 0);
  assert.match(none.stdout, /No Loom token found/);
  const down = await launch("loom-brief.mjs", ["list"], { LOOM_BASE_URL: UNREACHABLE, LOOM_API_KEY: TOKEN });
  assert.equal(down.code, 0);
  assert.match(down.stdout, /Loom did not answer/);
  const loom = await fakeLoom();
  try {
    const ok = await launch("loom-brief.mjs", ["list", "--all"], { LOOM_BASE_URL: loom.url, LOOM_API_KEY: TOKEN });
    assert.equal(ok.code, 0, ok.stderr);
    assert.match(ok.stdout, /1\. auto\/p\/main — rev 2/);
    const bad = await launch("loom-brief.mjs", ["save"], { LOOM_BASE_URL: loom.url, LOOM_API_KEY: TOKEN }, "{nope");
    assert.equal(bad.code, 2, "save without JSON sections is a usage error");
  } finally {
    await loom.close();
  }
  for (const r of [none, down]) assert.equal(r.stderr, "");
});

test("loom-client-status: exit 1 without a token or a server, 0 when every check passes", async () => {
  const none = await launch("loom-client-status.mjs", [], { LOOM_BASE_URL: UNREACHABLE });
  assert.equal(none.code, 1);
  const noneReport = JSON.parse(none.stdout);
  assert.equal(noneReport.ok, false);
  assert.equal(noneReport.checks.find((c) => c.name === "token").grade, "FAIL");
  const down = await launch("loom-client-status.mjs", [], { LOOM_BASE_URL: UNREACHABLE, LOOM_API_KEY: TOKEN });
  assert.equal(down.code, 1);
  assert.equal(JSON.parse(down.stdout).checks.find((c) => c.name === "server").grade, "FAIL");
  assert.ok(!down.stdout.includes("sig-not-printed"), "the token is never printed");
  const loom = await fakeLoom();
  try {
    const ok = await launch("loom-client-status.mjs", [], {
      LOOM_BASE_URL: loom.url,
      LOOM_API_KEY: TOKEN,
      LOOM_NAMESPACE: "ns",
    });
    assert.equal(ok.code, 0, ok.stdout);
    assert.equal(JSON.parse(ok.stdout).ok, true);
  } finally {
    await loom.close();
  }
});

test("loom-mcp-headers: one JSON object on stdout in every case", async () => {
  const none = await launch("loom-mcp-headers.mjs", [], { LOOM_BASE_URL: UNREACHABLE });
  assert.equal(none.code, 0);
  assert.deepEqual(JSON.parse(none.stdout), {});
  const tok = await launch("loom-mcp-headers.mjs", [], { LOOM_BASE_URL: UNREACHABLE, LOOM_API_KEY: TOKEN });
  assert.equal(tok.code, 0);
  assert.deepEqual(JSON.parse(tok.stdout), { Authorization: `Bearer ${TOKEN}` });
  assert.equal(tok.stderr, "");
});
