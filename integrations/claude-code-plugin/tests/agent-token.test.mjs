// An agent with no Loom token of its own: its workload token is exchanged for
// a Loom token, saved 0600 for that proof only, and renewed when it is due.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

process.env.LOOM_HOOK_NO_MAIN = "1";
const { agentToken, agentTokenPath, proofHash, resolveToken } = await import("../lib/loom-client.mjs");
const { exchange } = await import("../bin/loom-agent-token.mjs");

const SERVER = "https://loom.example";

function jwt(claims) {
  const part = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${part({ alg: "ES256" })}.${part(claims)}.sig`;
}
const A1 = jwt({ iss: "https://issuer.example", sub: "tenants/t/agents/a1" });

function agentEnv() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-agent-"));
  process.env.LOOM_BASE_URL = SERVER;
  return { HOME: home, LOOM_CLIENT_ID: "fleet", LOOM_WORKLOAD_TOKEN: A1 };
}

function save(env, saved) {
  fs.mkdirSync(path.dirname(agentTokenPath(env)), { recursive: true });
  fs.writeFileSync(agentTokenPath(env), JSON.stringify({ proof: proofHash(env), ...saved }));
}

const now = () => Date.now() / 1000;

test("no client id or workload token: not an agent", () => {
  const env = agentEnv();
  assert.equal(agentToken({ env: { HOME: env.HOME, LOOM_CLIENT_ID: "fleet" }, renew: assert.fail }), "");
});

test("a saved token with time left is used as it is", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now() + 3000 });
  assert.equal(agentToken({ env, renew: assert.fail }), "t1");
});

test("one about to run out is renewed first", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now() + 60 });
  const renew = () => save(env, { server: SERVER, client_id: "fleet", token: "t2", exp: now() + 3600 });
  assert.equal(agentToken({ env, renew }), "t2");
});

test("a five-minute token is renewed halfway, not on every call", () => {
  const env = agentEnv();
  const t = now();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: t + 200, ttl: 300 });
  assert.equal(agentToken({ env, renew: assert.fail }), "t1");
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: t + 100, ttl: 300 });
  const renew = () => save(env, { server: SERVER, client_id: "fleet", token: "t2", exp: t + 300, ttl: 300 });
  assert.equal(agentToken({ env, renew }), "t2");
});

test("a renewal that fails keeps the token until its last half-minute", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now() + 60 });
  assert.equal(agentToken({ env, renew: () => {} }), "t1");
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: now() + 10 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
});

test("a failed renewal is not tried again on every call", () => {
  const env = agentEnv();
  const t = now();
  save(env, { server: SERVER, client_id: "fleet", token: "t1", exp: t + 20 });
  let tries = 0;
  const renew = () => tries++;
  for (let i = 0; i < 5; i++) agentToken({ env, renew, now: t });
  assert.equal(tries, 1);
  agentToken({ env, renew, now: t + 31 });
  assert.equal(tries, 2);
});

test("a token saved for another server or trust is never sent here", () => {
  const env = agentEnv();
  save(env, { server: "https://elsewhere.example", client_id: "fleet", token: "x", exp: now() + 3000 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
  save(env, { server: SERVER, client_id: "other", token: "x", exp: now() + 3000 });
  assert.equal(agentToken({ env, renew: () => {} }), "");
});

test("a saved token serves only the proof it was exchanged for", () => {
  const env = agentEnv();
  save(env, { server: SERVER, client_id: "fleet", token: "of-a1", exp: now() + 3000 });
  // Another agent sharing the home, even one with the same iss and sub (a
  // trust telling agents apart by agent_id), exchanges its own proof.
  const b = {
    ...env,
    LOOM_WORKLOAD_TOKEN: jwt({ iss: "https://issuer.example", sub: "tenants/t/agents/a1", agent_id: "b" }),
  };
  let renewed = 0;
  assert.equal(agentToken({ env: b, renew: () => renewed++ }), "");
  assert.equal(renewed, 1);
  // Its failed exchange marks a retry for its own proof: A's token stays A's.
  assert.equal(agentToken({ env: b, renew: () => renewed++ }), "");
  assert.equal(agentToken({ env, renew: assert.fail }), "of-a1");
});

test("an agent whose exchange fails signs in as no one, not as a person", () => {
  const env = agentEnv();
  const cfg = path.join(env.HOME, ".loom", "config.json");
  fs.mkdirSync(path.dirname(cfg), { recursive: true });
  fs.writeFileSync(cfg, JSON.stringify({ token: "a-persons-token" }));
  const set = { ...env, LOOM_CONFIG_FILE: cfg, LOOM_CLIENT_ID: "fleet-unknown" };
  for (const [k, v] of Object.entries(set)) process.env[k] = v;
  try {
    assert.equal(resolveToken(), "");
  } finally {
    for (const k of ["LOOM_CLIENT_ID", "LOOM_WORKLOAD_TOKEN", "LOOM_CONFIG_FILE"]) delete process.env[k];
  }
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
  assert.equal(sent.form.subject_token, A1);
  const saved = JSON.parse(fs.readFileSync(agentTokenPath(env), "utf8"));
  assert.deepEqual(saved, {
    server: SERVER,
    client_id: "fleet",
    proof: proofHash(env),
    token: "t1",
    exp: 4600,
    ttl: 3600,
  });
  assert.equal(fs.statSync(agentTokenPath(env)).mode & 0o777, 0o600);
});

test("the saved token names the proof sent, though the file rotated meanwhile", async () => {
  const env = agentEnv();
  const file = path.join(env.HOME, "workload");
  fs.writeFileSync(file, A1);
  const fileEnv = { HOME: env.HOME, LOOM_CLIENT_ID: "fleet", LOOM_WORKLOAD_TOKEN_FILE: file };
  const B = jwt({ iss: "https://issuer.example", sub: "tenants/t/agents/b" });
  const fetchImpl = async () => {
    fs.writeFileSync(file, B); // rotated while the exchange was in flight
    return new Response(JSON.stringify({ access_token: "of-a1", expires_in: 3600 }), { status: 200 });
  };
  assert.equal(await exchange({ env: fileEnv, fetchImpl }), true);
  const saved = JSON.parse(fs.readFileSync(agentTokenPath(fileEnv), "utf8"));
  assert.equal(saved.proof, proofHash(fileEnv, A1));
});

test("a refused exchange saves nothing", async () => {
  const env = agentEnv();
  const fetchImpl = async () => new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
  assert.equal(await exchange({ env, fetchImpl }), false);
  assert.equal(fs.existsSync(agentTokenPath(env)), false);
});

test("a redirect from the token endpoint is never followed", async () => {
  const env = agentEnv();
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, redirect: init.redirect });
    return new Response(null, { status: 307, headers: { location: "https://elsewhere.example/collect" } });
  };
  assert.equal(await exchange({ env, fetchImpl, now: 1000 }), false);
  assert.deepEqual(seen, [{ url: `${SERVER}/oauth/token`, redirect: "manual" }]);
  assert.equal(fs.existsSync(agentTokenPath(env)), false);
});
