// An agent with no Loom token of its own: its workload token is exchanged
// for an hour-long one, saved 0600, and renewed only when it is about to run out.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

process.env.LOOM_HOOK_NO_MAIN = "1";
const { agentToken, agentTokenPath } = await import("../lib/loom-client.mjs");
const { exchange } = await import("../bin/loom-agent-token.mjs");

const SERVER = "https://loom.example";

function agentEnv() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-agent-"));
  process.env.LOOM_BASE_URL = SERVER;
  return { HOME: home, LOOM_CLIENT_ID: "fleet", LOOM_WORKLOAD_TOKEN: "oidc-of-a1" };
}

function save(env, saved) {
  fs.mkdirSync(path.dirname(agentTokenPath(env)), { recursive: true });
  fs.writeFileSync(agentTokenPath(env), JSON.stringify(saved));
}

test("no client id or workload token: not an agent", () => {
  const env = agentEnv();
  assert.equal(agentToken({ env: { HOME: env.HOME, LOOM_CLIENT_ID: "fleet" }, renew: assert.fail }), "");
});

test("a saved token with time left is used as it is", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: Date.now() / 1000 + 3000 });
  assert.equal(agentToken({ env, renew: assert.fail }), "t1");
});

test("one about to run out is renewed first", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: Date.now() / 1000 + 60 });
  const renew = () => save(env, { server: SERVER, client_id: "fleet", token: "t2", exp: Date.now() / 1000 + 3600 });
  assert.equal(agentToken({ env, renew }), "t2");
});

test("a five-minute token is renewed halfway, not on every call", () => {
  const env = agentEnv();
  const now = Date.now() / 1000;
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now + 200, ttl: 300 });
  assert.equal(agentToken({ env, renew: assert.fail }), "t1");
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now + 100, ttl: 300 });
  const renew = () => save(env, { server: SERVER, client_id: "fleet", token: "t2", exp: now + 300, ttl: 300 });
  assert.equal(agentToken({ env, renew }), "t2");
});

test("a renewal that fails keeps the token until its last half-minute", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: Date.now() / 1000 + 60 });
  assert.equal(agentToken({ env, renew: () => {} }), "t1");
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: Date.now() / 1000 + 10 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
});

test("a token saved for another server or trust is never sent here", () => {
  const env = agentEnv();
  save(env, { server: "https://elsewhere.example", client_id: "fleet", token: "x", exp: Date.now() / 1000 + 3000 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
  save(env, { server: SERVER, client_id: "other", token: "x", exp: Date.now() / 1000 + 3000 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
});

test("the exchange sends the workload token and saves the answer 0600", async () => {
  const env = agentEnv();
  let sent;
  const fetchImpl = async (url, init) => {
    sent = { url, form: Object.fromEntries(new URLSearchParams(init.body)) };
    return new Response(JSON.stringify({ access_token: "t1", expires_in: 3600 }), { status: 200 });
  };
  assert.equal(await exchange({ env, fetchImpl, now: 1000 }), true);
  assert.equal(sent.url, `${SERVER}/oauth/token`);
  assert.equal(sent.form.grant_type, "urn:ietf:params:oauth:grant-type:token-exchange");
  assert.equal(sent.form.client_id, "fleet");
  assert.equal(sent.form.subject_token, "oidc-of-a1");
  const saved = JSON.parse(fs.readFileSync(agentTokenPath(env), "utf8"));
  assert.deepEqual(saved, { server: SERVER, client_id: "fleet", token: "t1", exp: 4600, ttl: 3600 });
  assert.equal(fs.statSync(agentTokenPath(env)).mode & 0o777, 0o600);
});

test("a refused exchange saves nothing", async () => {
  const env = agentEnv();
  const fetchImpl = async () => new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
  assert.equal(await exchange({ env, fetchImpl }), false);
  assert.equal(fs.existsSync(agentTokenPath(env)), false);
});
