import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

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
  const { loomConfigPath } = await import("../lib/loom-client.mjs");
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
  const none = await run("", "", "");
  assert.equal(
    none.checks.some((c) => c.name === "source"),
    false,
  );
});
