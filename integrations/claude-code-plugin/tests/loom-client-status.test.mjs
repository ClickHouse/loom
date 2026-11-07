import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

process.env.LOOM_HOOK_NO_MAIN = "1";
const { clientStatus } = await import("../bin/loom-client-status.mjs");

const scope = { org: "test-org", namespace: "work" };
const serverUrl = "https://loom.test";
const now = Date.UTC(2026, 8, 25);
const token = (exp = Math.floor(now / 1000) + 3600) =>
  `a.${Buffer.from(JSON.stringify({ sub: "tester", org: "test-org", exp })).toString("base64url")}.c`;

function responses(authStatus = 200, role = "writer") {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    const body = url.endsWith("/v1/health")
      ? { build_commit: "abc123" }
      : { rows: [{ namespace: "work", role, status: "active" }] };
    return { status: url.endsWith("/v1/health") ? 200 : authStatus, json: async () => body };
  };
  return { calls, fetchImpl };
}

test("plugin-only status checks server, bearer and write grant without a test write", async () => {
  const { calls, fetchImpl } = responses();
  const result = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl });
  assert.equal(result.ok, true);
  assert.deepEqual(
    result.checks.map((c) => [c.name, c.grade]),
    [
      ["server", "PASS"],
      ["token", "PASS"],
      ["auth", "PASS"],
      ["namespace", "PASS"],
      ["flush", "PASS"],
    ],
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[1].url, `${serverUrl}/v1/namespace.list_mine`);
  assert.equal(calls[1].options.headers.authorization, `Bearer ${token()}`);
  assert.deepEqual(JSON.parse(calls[1].options.body), { org: "test-org" });
  assert.equal(JSON.stringify(result).includes(token()), false);
});

test("status distinguishes an expired token, rejected bearer and viewer grant", async () => {
  const expired = responses();
  const a = await clientStatus({
    serverUrl,
    token: token(Math.floor(now / 1000) - 1),
    scope,
    now,
    fetchImpl: expired.fetchImpl,
  });
  assert.equal(a.checks.find((c) => c.name === "token").grade, "FAIL");
  assert.equal(expired.calls.length, 1);

  const rejected = responses(401);
  const b = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl: rejected.fetchImpl });
  assert.equal(b.checks.find((c) => c.name === "auth").grade, "FAIL");

  const viewer = responses(200, "viewer");
  const c = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl: viewer.fetchImpl });
  assert.equal(c.checks.find((x) => x.name === "namespace").grade, "FAIL");

  const unknown = responses(404);
  const d = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl: unknown.fetchImpl });
  assert.equal(d.ok, false, "an unverified grant is not a healthy client");
  assert.equal(d.checks.find((x) => x.name === "auth").grade, "WARN");
});

test("status reports stranded capture jobs without reading their contents", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-status-flush-"));
  try {
    const job = path.join(dir, "loom-flush-1790320000000-123.json");
    fs.writeFileSync(job, "sensitive content never read");
    fs.utimesSync(job, new Date(now - 900_000), new Date(now - 900_000));
    const { fetchImpl } = responses();
    const result = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl, tempDirectory: dir });
    assert.equal(result.checks.find((c) => c.name === "flush").grade, "WARN");
    assert.equal(JSON.stringify(result).includes("sensitive content"), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("an agent with no token is told its workload-token exchange failed", async () => {
  const saved = { id: process.env.LOOM_CLIENT_ID, wl: process.env.LOOM_WORKLOAD_TOKEN };
  process.env.LOOM_CLIENT_ID = "ci";
  process.env.LOOM_WORKLOAD_TOKEN = "proof";
  try {
    const { fetchImpl } = responses();
    const result = await clientStatus({ serverUrl, token: "", scope, now, fetchImpl });
    const check = result.checks.find((c) => c.name === "token");
    assert.equal(check.grade, "FAIL");
    assert.match(check.detail, /workload-token exchange at https:\/\/loom\.test failed/);
  } finally {
    for (const [k, v] of [
      ["LOOM_CLIENT_ID", saved.id],
      ["LOOM_WORKLOAD_TOKEN", saved.wl],
    ]) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
});

test("the source line sits between token and auth, names the lookup, and warns when the config names another server", async () => {
  const { loomConfigPath, loomTokenPath } = await import("../lib/loom-client.mjs");
  const run = (tokenKind, configUrl, tok = token()) =>
    clientStatus({ serverUrl, token: tok, scope, now, fetchImpl: responses().fetchImpl, tokenKind, configUrl });
  const pass = await run("env", "https://elsewhere");
  assert.deepEqual(
    pass.checks.map((c) => c.name),
    ["server", "token", "source", "auth", "namespace", "flush"],
  );
  assert.deepEqual(pass.checks[2], { name: "source", grade: "PASS", detail: "token from LOOM_API_KEY" });
  const warn = await run("config", "https://elsewhere/");
  const line = warn.checks.find((c) => c.name === "source");
  assert.equal(line.grade, "WARN");
  assert.match(line.detail, /names https:\/\/elsewhere; this check uses https:\/\/loom\.test/);
  assert.ok(line.detail.includes(loomConfigPath()));
  assert.equal(warn.ok, true); // a WARN never fails the check
  assert.equal((await run("config", `${serverUrl}/`)).checks[2].grade, "PASS");
  assert.equal((await run("file", "https://elsewhere")).checks[2].detail, `token from ${loomTokenPath()}`);
  assert.equal((await run("agent", "https://elsewhere")).checks[2].detail, "token from the workload-token exchange");
  const none = await run("", "", "");
  assert.equal(
    none.checks.some((c) => c.name === "source"),
    false,
  );
});

test("the agent-mode line names the server the exchange went to, not the one checked", async () => {
  const { fetchImpl } = responses();
  const saved = { ...process.env };
  process.env.LOOM_CLIENT_ID = "ci";
  process.env.LOOM_WORKLOAD_TOKEN = "proof";
  try {
    const r = await clientStatus({ serverUrl, token: "", scope, now, fetchImpl, exchangeUrl: "https://hooks.test" });
    assert.match(r.checks.find((c) => c.name === "token").detail, /exchange at https:\/\/hooks\.test failed/);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("the Codex copy checks the hooks' server and makes no source comparison", async () => {
  // Codex's MCP entry may be replaced whole in Codex's own config, which the
  // script cannot read: it checks the hooks' server and the source line only
  // names the lookup.
  const { spawn } = await import("node:child_process");
  const codexScript = fileURLToPath(new URL("../../codex-plugin/bin/loom-client-status.mjs", import.meta.url));
  const home = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-status-codex-")));
  fs.mkdirSync(path.join(home, ".loom"));
  // A token the spawned process's own clock reads as live: the server line
  // and the source line are what this test is about, not expiry.
  const live = token(Math.floor(Date.now() / 1000) + 3600);
  fs.writeFileSync(
    path.join(home, ".loom", "config.json"),
    JSON.stringify({ url: "https://elsewhere.test", token: live }),
  );
  try {
    const out = await new Promise((resolve) => {
      const child = spawn(process.execPath, [codexScript], {
        env: { PATH: process.env.PATH, HOME: home, TMPDIR: home, LOOM_BASE_URL: "http://127.0.0.1:9" },
      });
      let stdout = "";
      child.stdout.on("data", (c) => (stdout += c));
      child.on("close", () => resolve(stdout));
    });
    const result = JSON.parse(out);
    assert.match(result.checks.find((c) => c.name === "server").detail, /127\.0\.0\.1:9/);
    const source = result.checks.find((c) => c.name === "source");
    assert.equal(source.grade, "PASS");
    assert.ok(source.detail.endsWith("config.json"), source.detail);
    assert.equal(result.checks.find((c) => c.name === "token").grade, "PASS");
    assert.equal(out.includes(live), false);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("status warns, without failing, about a server older than the plugin's minimum", async () => {
  const fetchImpl = async (url) => ({
    status: 200,
    json: async () =>
      url.endsWith("/v1/health")
        ? { build_commit: "abc123", version: "0.0.1" }
        : { rows: [{ namespace: "work", role: "writer", status: "active" }] },
  });
  const result = await clientStatus({ serverUrl, token: token(), scope, now, fetchImpl });
  assert.equal(result.ok, true);
  const check = result.checks.find((c) => c.name === "server version");
  assert.equal(check.grade, "WARN");
  assert.ok(check.detail.includes("runs 0.0.1") && check.detail.includes("upgrade the server"), check.detail);
});
