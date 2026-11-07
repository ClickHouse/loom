// The first session signs itself up.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ensureCredential, planSignup, renderFirstSessionNotice, signupName } from "../lib/first-session.mjs";
import { writeConfigMerged } from "../lib/loom-client.mjs";
import { assertMode } from "./host-env.mjs";

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "loom-first-"));
}
const ok = (body) => async () => ({ ok: true, status: 200, json: async () => body });
const status = (code) => async () => ({ ok: false, status: code, json: async () => ({}) });
const SIGNED = {
  token: "tok.en",
  namespace: "agent-alice-box-claude-code",
  org: "dev",
  subject: "sa:agent-alice-box-claude-code-1234",
  claim_code: "SECRET-CODE",
  claim_url: "https://loom.example/",
};

test("planSignup: token present or LOOM_AUTO_SIGNUP=off never signs up; otherwise it runs", () => {
  assert.equal(planSignup({}, { hasToken: true }).run, false);
  assert.equal(planSignup({ LOOM_AUTO_SIGNUP: "off" }, { hasToken: false }).run, false);
  assert.equal(planSignup({ LOOM_AUTO_SIGNUP: "0" }, { hasToken: false }).run, false);
  assert.equal(planSignup({}, { hasToken: false }).run, true);
});

test("signupName: user-host-client slug, lower-case, bounded", () => {
  assert.equal(
    signupName({ user: "Alice", host: "Alice’s MacBook.local", client: "codex" }),
    "alice-alice-s-macbook-local-codex",
  );
  assert.equal(signupName({ user: "", host: "", client: "" }), "coding-agent");
  assert.ok(signupName({ user: "x".repeat(80), host: "h", client: "claude-code" }).length <= 60);
});

test("created: token to config.json (0600, merged), claim code to claim file (0600), never in the notice", async () => {
  const dir = tmp();
  const configPath = path.join(dir, "config.json");
  fs.writeFileSync(configPath, JSON.stringify({ url: "https://old", keep: 1 }));
  const r = await ensureCredential({
    env: {},
    paths: { configPath },
    fetchImpl: ok(SIGNED),
    serverUrl: "https://loom.example/",
    client: "claude-code",
    host: "box",
    user: "alice",
    now: () => new Date("2026-09-17T17:00:00Z"),
  });
  assert.equal(r.status, "created");
  const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
  assert.equal(cfg.token, "tok.en");
  assert.equal(cfg.namespace, SIGNED.namespace);
  assert.equal(cfg.url, "https://loom.example");
  assert.equal(cfg.keep, 1, "existing keys survive");
  assert.equal(cfg.claimed, false);
  assert.equal(cfg.signed_up_by, "claude-code");
  assertMode(configPath, 0o600);
  assert.equal(r.claimPath, path.join(dir, `claim-${SIGNED.namespace}.txt`));
  const claim = fs.readFileSync(r.claimPath, "utf8");
  assert.match(claim, /SECRET-CODE/);
  assert.match(claim, /https:\/\/loom\.example\//);
  assertMode(r.claimPath, 0o600);
  const notice = renderFirstSessionNotice(r, "https://loom.example");
  assert.match(notice, /agent-alice-box-claude-code/);
  assert.match(notice, /claim-agent-alice-box-claude-code\.txt/);
  assert.doesNotMatch(notice, /SECRET-CODE/);
  assert.doesNotMatch(notice, /tok\.en/);
});

test("404 → unavailable (hand instructions), 429 → rate_limited, 500 → error; nothing written", async () => {
  for (const [code, want] of [
    [404, "unavailable"],
    [429, "rate_limited"],
    [500, "error"],
  ]) {
    const dir = tmp();
    const configPath = path.join(dir, "config.json");
    const r = await ensureCredential({
      env: {},
      paths: { configPath },
      fetchImpl: status(code),
      serverUrl: "https://loom.example",
      host: "box",
      user: "alice",
    });
    assert.equal(r.status, want);
    assert.equal(fs.existsSync(configPath), false);
  }
  assert.match(
    renderFirstSessionNotice({ status: "unavailable" }, "https://loom.example"),
    /https:\/\/loom\.example\/ \(Settings → Tokens\)/,
  );
  assert.equal(renderFirstSessionNotice({ status: "off" }, "x"), "");
});

test("network failure and a bad body are errors, not throws", async () => {
  const boom = async () => {
    throw new Error("ECONNREFUSED");
  };
  assert.equal(
    (
      await ensureCredential({
        env: {},
        paths: { configPath: path.join(tmp(), "c.json") },
        fetchImpl: boom,
        serverUrl: "https://x",
      })
    ).status,
    "error",
  );
  assert.equal(
    (
      await ensureCredential({
        env: {},
        paths: { configPath: path.join(tmp(), "c.json") },
        fetchImpl: ok({}),
        serverUrl: "https://x",
      })
    ).status,
    "error",
  );
});

test("writeConfigMerged creates the directory 0700 and the file 0600", () => {
  const dir = path.join(tmp(), "nested");
  const configPath = path.join(dir, "config.json");
  writeConfigMerged(configPath, { a: 1 });
  assertMode(dir, 0o700);
  assert.deepEqual(JSON.parse(fs.readFileSync(configPath, "utf8")), { a: 1 });
});

// The MCP header helper and the SessionStart hook sign up
// through one lock — the first signs up, the second reuses its token.
test("two concurrent callers create ONE service account; the second reuses the first's token", async () => {
  const dir = tmp();
  const configPath = path.join(dir, "config.json");
  let calls = 0;
  const slow = async () => {
    calls += 1;
    await new Promise((r) => setTimeout(r, 150));
    return { ok: true, status: 200, json: async () => SIGNED };
  };
  const args = {
    env: {},
    paths: { configPath },
    fetchImpl: slow,
    serverUrl: "https://loom.example",
    client: "claude-code",
    host: "box",
    user: "alice",
  };
  const [a, b] = await Promise.all([ensureCredential(args), ensureCredential(args)]);
  assert.equal(calls, 1, "one sign-up request");
  assert.equal(a.token, "tok.en");
  assert.equal(b.token, "tok.en");
  assert.equal(a.status, "created");
  assert.equal(b.status, "created");
  assert.ok([a.by, b.by].includes("peer"), "one result names the peer");
  assert.equal(b.claimPath || a.claimPath, path.join(dir, `claim-${SIGNED.namespace}.txt`));
  assert.equal(fs.existsSync(path.join(dir, ".signup.lock")), false, "lock released");
});

test("a stale lock (crashed signer) is taken over; a fresh one is waited on, then reported", async () => {
  const dir = tmp();
  const configPath = path.join(dir, "config.json");
  const lock = path.join(dir, ".signup.lock");
  fs.writeFileSync(lock, "");
  const old = new Date(Date.now() - 10 * 60 * 1000);
  fs.utimesSync(lock, old, old);
  const r = await ensureCredential({
    env: {},
    paths: { configPath },
    fetchImpl: ok(SIGNED),
    serverUrl: "https://loom.example",
    host: "box",
    user: "alice",
  });
  assert.equal(r.status, "created");
  assert.equal(r.by, undefined);
  // A live lock with no token ever written: the waiter gives up with an error, it never signs up itself.
  const dir2 = tmp();
  fs.writeFileSync(path.join(dir2, ".signup.lock"), "");
  let t = 0;
  const clock = () => new Date(1_000_000 + t);
  const r2 = await ensureCredential({
    env: {},
    paths: { configPath: path.join(dir2, "config.json") },
    fetchImpl: ok(SIGNED),
    serverUrl: "https://loom.example",
    now: clock,
    sleep: async () => {
      t += 1000;
    },
  });
  assert.equal(r2.status, "error");
  assert.match(r2.reason, /another process/);
});

test("single-user server: token saved, no claim file, notice says connected", async () => {
  const dir = tmp();
  const configPath = path.join(dir, "config.json");
  const r = await ensureCredential({
    env: {},
    paths: { configPath },
    fetchImpl: ok({ ...SIGNED, namespace: "local", subject: "you", claim_code: "" }),
    serverUrl: "http://localhost:7777",
    client: "claude-code",
    host: "box",
    user: "alice",
  });
  assert.equal(r.status, "created");
  assert.equal(r.claimed, true);
  assert.equal(r.claimPath, "");
  assert.equal(JSON.parse(fs.readFileSync(configPath, "utf8")).claimed, true);
  assert.deepEqual(
    fs.readdirSync(dir).filter((f) => f.startsWith("claim-")),
    [],
  );
  const notice = renderFirstSessionNotice(r, "http://localhost:7777");
  assert.match(notice, /connected this machine to http:\/\/localhost:7777/);
  assert.doesNotMatch(notice, /claim/i);
});

test("an agent whose exchange failed is told so once, not left silent", () => {
  assert.equal(
    planSignup({ LOOM_CLIENT_ID: "ci", LOOM_WORKLOAD_TOKEN: "w" }, { hasToken: false }).reason,
    "agent mode",
  );
  const notice = renderFirstSessionNotice({ status: "off", reason: "agent mode" }, "https://loom.example");
  assert.match(notice, /workload token could not be exchanged at https:\/\/loom\.example/);
  assert.equal(renderFirstSessionNotice({ status: "off", reason: "LOOM_AUTO_SIGNUP=off" }, "x"), "");
});
