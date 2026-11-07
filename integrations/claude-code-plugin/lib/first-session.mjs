// The first session signs itself up.
//
// At the first SessionStart without a credential, the plugin calls the
// server's agent self-signup: the server provisions a memory
// namespace for this machine + client, grants it writer, and returns a
// token plus a claim code that works once. The token goes to ~/.loom/config.json
// (0600), the claim code to ~/.loom/claim-<namespace>.txt (0600) — never
// into the model's context. A person makes the namespace theirs at the claim
// page (`claimUrl`) with the namespace name, the code and a password of their own.
//
// LOOM_AUTO_SIGNUP=off disables. A server with self-signup off (404) or a
// rate limit (429) leaves the machine without a credential: hooks no-op, and
// the SessionStart primer says how to get a token by hand.
import { closeSync, existsSync, mkdirSync, openSync, statSync, unlinkSync } from "node:fs";
import { hostname, userInfo } from "node:os";
import { dirname, join } from "node:path";

import {
  agentMode,
  envSwitch,
  loomConfigPath,
  readConfigFile,
  requestJson,
  writeConfigMerged,
  writePrivateFile,
} from "./loom-client.mjs";

export const SIGNUP_TIMEOUT_MS = 8000;
/** A lock older than this is a crashed signer's; the next caller takes it. */
export const SIGNUP_LOCK_STALE_MS = SIGNUP_TIMEOUT_MS * 4;

// The MCP header helper and the SessionStart hook both run at session start
// and neither waits for the other. A helper that answered with no bearer
// while the hook signed up would send Claude Code into OAuth discovery (an
// "HTTP 404 dialing …/mcp"), so both sign up through one lock beside
// config.json: the first caller signs up, a second waits for the token it
// writes, and one service account results.
function _tryLock(lockPath, nowMs) {
  mkdirSync(dirname(lockPath), { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      closeSync(openSync(lockPath, "wx", 0o600));
      return true;
    } catch (e) {
      if (!e || e.code !== "EEXIST") return false;
      try {
        if (nowMs() - statSync(lockPath).mtimeMs <= SIGNUP_LOCK_STALE_MS) return false;
        unlinkSync(lockPath);
      } catch {
        return false;
      }
    }
  }
  return false;
}

/** The credential another process just wrote, shaped as a sign-up result so
 *  the primer still tells the person how to claim the namespace. */
function _peerResult(cfg, { base, dir, configPath }) {
  const namespace = String(cfg.namespace || "");
  return {
    status: "created",
    by: "peer",
    token: String(cfg.token),
    namespace,
    org: cfg.org || "",
    claimUrl: `${String(cfg.url || base).replace(/\/+$/, "")}/`,
    claimPath: cfg.claimed === true ? "" : join(dir, `claim-${namespace}.txt`),
    claimed: cfg.claimed === true,
    configPath,
  };
}

async function _awaitPeerToken(configPath, lockPath, ctx, { nowMs, sleep, waitMs }) {
  const deadline = nowMs() + waitMs;
  while (nowMs() < deadline) {
    const cfg = readConfigFile(configPath);
    if (String(cfg.token || "").trim()) return _peerResult(cfg, ctx);
    if (!existsSync(lockPath)) break; // the signer finished without a token
    await sleep(100);
  }
  const cfg = readConfigFile(configPath);
  if (String(cfg.token || "").trim()) return _peerResult(cfg, ctx);
  return { status: "error", reason: "sign-up in progress in another process" };
}

/** Whether to attempt a sign-up: never with a token, never for an agent
 *  (`agentMode`: it signs in as itself or not at all), never when switched off. */
export function planSignup(env, { hasToken }) {
  if (hasToken) return { run: false, reason: "token present" };
  // An agent signs in as itself or not at all: never as the token a person
  // saved here, nor as a new sign-up.
  if (agentMode(env)) return { run: false, reason: "agent mode" };
  if (!envSwitch("LOOM_AUTO_SIGNUP", true, env)) return { run: false, reason: "LOOM_AUTO_SIGNUP=off" };
  return { run: true, reason: "no token" };
}

/** `<user>-<host>-<client>`, lower-case, [a-z0-9-], at most 60 chars (the
 *  server prefixes `agent-` and allows 64). */
export function signupName({ user, host, client }) {
  const raw = [user, host, client]
    .map((s) =>
      String(s || "")
        .trim()
        .toLowerCase(),
    )
    .filter(Boolean)
    .join("-");
  const slug = raw
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
  return (slug || "coding-agent").slice(0, 60).replace(/-$/, "");
}

/**
 * Sign this machine up when it has no credential. Never throws; the result
 * says what happened:
 *   { status: "present" | "off" }                       — nothing done
 *   { status: "created", token, namespace, org, claimUrl, claimPath, configPath }
 *   { status: "unavailable" | "rate_limited" | "error", http? }
 */
export async function ensureCredential({
  env = process.env,
  paths = {},
  fetchImpl = fetch,
  hasToken = false,
  client = "claude-code",
  serverUrl,
  host = hostname(),
  user = (() => {
    try {
      return userInfo().username;
    } catch {
      return "user";
    }
  })(),
  now = () => new Date(),
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
} = {}) {
  const plan = planSignup(env, { hasToken });
  if (!plan.run) return { status: hasToken ? "present" : "off", reason: plan.reason };
  const base = String(serverUrl || "").replace(/\/+$/, "");
  if (!base) return { status: "error", reason: "no server url" };
  const configPath = paths.configPath || loomConfigPath(env);
  const dir = dirname(configPath);
  const lockPath = join(dir, ".signup.lock");
  const nowMs = () => now().getTime();
  const ctx = { base, dir, configPath };
  if (!_tryLock(lockPath, nowMs)) {
    return _awaitPeerToken(configPath, lockPath, ctx, { nowMs, sleep, waitMs: SIGNUP_TIMEOUT_MS + 2000 });
  }
  try {
    const cfg = readConfigFile(configPath);
    if (String(cfg.token || "").trim()) return _peerResult(cfg, ctx);
    return await _signUp({ base, client, host, user, fetchImpl, now, configPath, dir, paths });
  } finally {
    try {
      unlinkSync(lockPath);
    } catch {
      /* already gone */
    }
  }
}

async function _signUp({ base, client, host, user, fetchImpl, now, configPath, dir, paths }) {
  const name = signupName({ user, host, client });
  const res = await requestJson(`${base}/v1/auth/agent-signup`, {
    body: { name, purpose: `${client} plugin, first session on ${host}`, ttl_days: 365 },
    timeoutMs: SIGNUP_TIMEOUT_MS,
    fetchImpl,
  });
  if (!res.status) return { status: "error", reason: res.error || "no response" };
  if (res.status === 404) return { status: "unavailable", http: 404 };
  if (res.status === 429) return { status: "rate_limited", http: 429 };
  if (res.status < 200 || res.status >= 300) return { status: "error", http: res.status };
  const body = res.body;
  if (!body || typeof body !== "object") return { status: "error", reason: "bad json" };
  const token = String(body.token || "").trim();
  const namespace = String(body.namespace || "").trim();
  if (!token || !namespace) return { status: "error", reason: "incomplete signup body" };
  // A single-user server returns its one identity: nothing to claim.
  const claimCode = String(body.claim_code || "");
  const claimed = !claimCode;
  writeConfigMerged(configPath, {
    url: base,
    token,
    namespace,
    org: body.org || undefined,
    subject: body.subject || undefined,
    signed_up_at: now().toISOString(),
    signed_up_by: client,
    claimed,
  });
  const claimUrl = String(body.claim_url || `${base}/`);
  const claimPath = claimed ? "" : paths.claimPath || join(dir, `claim-${namespace}.txt`);
  if (!claimed) {
    writePrivateFile(
      claimPath,
      `Loom namespace ${namespace} on ${base} was created for this machine by the ${client} plugin.\n` +
        `To make it yours, open ${claimUrl} and enter the namespace name (${namespace}) and this code where it asks:\n` +
        `${claimCode}\n`,
    );
  }
  return { status: "created", token, namespace, org: body.org || "", claimUrl, claimPath, claimed, configPath };
}

/** The one line the primer prints about the credential — no secrets. */
export function renderFirstSessionNotice(result, serverUrl) {
  const base = String(serverUrl || "").replace(/\/+$/, "");
  switch (result && result.status) {
    case "created":
      if (result.claimed) {
        return `Loom: connected this machine to ${base}; memory goes to namespace \`${result.namespace}\`.`;
      }
      return (
        `Loom: no credential was found, so this machine signed itself up as memory namespace ` +
        `\`${result.namespace}\` on ${base}. Tell the user once: to make it theirs, open ${result.claimUrl} and ` +
        `enter namespace \`${result.namespace}\` and the code saved in ${result.claimPath} where it asks; ` +
        `until then memory is per-machine.`
      );
    case "unavailable":
      return (
        `Loom: no credential, and ${base} does not offer self-signup. Tell the user once: get a token at ` +
        `${base}/ (Settings → Tokens), then \`claude plugin install loom@loom --config token=…\` or save it to ~/.loom/token.`
      );
    case "rate_limited":
      return `Loom: no credential; ${base} rate-limited sign-up for this address — the next session retries.`;
    case "off":
      if (result.reason === "agent mode") {
        return `Loom: this agent's workload token could not be exchanged at ${base}; hooks stay silent this session.`;
      }
      return "";
    default:
      return `Loom: no credential and sign-up at ${base} failed; hooks stay silent this session.`;
  }
}
