#!/usr/bin/env node
// The MCP entry's headers helper. It authenticates from the places the hooks
// do, less the install option, which is not in its environment: LOOM_API_KEY;
// for an agent (LOOM_CLIENT_ID with a workload token) its exchanged token and
// nothing else; otherwise ~/.loom/config.json, then ~/.loom/token.
//
// It also names the code namespace of the session's checkout. SessionStart
// publishes its structured cwd in private state; the helper finds only the
// state of its own live client ancestor. A bounded lookup miss leaves the
// connection without an implicit checkout scope, as an unindexed checkout does.
// Claude Code may continue connecting when a headers helper fails.
//
// No token and not an agent → the helper waits for the SessionStart hook,
// which Claude Code runs while it connects the MCP entry and which knows the
// configured server, to save the first-session credential; only then does it
// sign up itself, through the same locked path, so one account results.
// Sign-up off, refused or unreachable → `{}`: the server then answers 401
// and Claude Code shows the connection as failed, which is the honest state.
// Stdout is exactly one JSON object; nothing else is written.
import { sessionProject } from "../lib/mcp-project.mjs";
import { isAbsolute } from "node:path";
import { ensureCredential } from "../lib/first-session.mjs";
import {
  agentMode,
  pluginDefaults,
  envNumber,
  envSwitch,
  gitToplevel,
  hookClient,
  loomConfigPath,
  readConfigFile,
  resolveServerUrl,
  resolveToken,
} from "../lib/loom-client.mjs";
import { readCache, resolveIndexedRepo, scopeHeader, scopeKey } from "../lib/repo-scope.mjs";

/** Bound for the helper's own lookup when the cache is empty: Claude Code
 *  waits on this process for the headers, so it is shorter than the shunt's.
 *  A miss costs only the header — the primer's scope line and the
 *  next shunt decision still carry the namespace. */
export const HELPER_LOOKUP_MS = 2500;

export function headersFor(token, scope = null) {
  const t = String(token || "").trim();
  const out = t ? { Authorization: `Bearer ${t}` } : {};
  return { ...out, ...scopeHeader(scope) };
}

/** The session's indexed repository for the project directory `dir`: the
 *  shared cache, else one bounded lookup (which also fills the cache).
 *  Outside a checkout the repository named by LOOM_CODE_REPO / `code_repo`
 *  stands in; null when there is neither, without a token, or when
 *  disabled. */
export async function checkoutScope(dir = process.cwd(), { env = process.env, token = "", post } = {}) {
  if (!envSwitch("LOOM_MCP_CODE_SCOPE", true, env)) return null;
  // Only absolute context directories: never resolve missing state against
  // the plugin's own checkout.
  const root = isAbsolute(dir) ? gitToplevel(dir) : "";
  const key = scopeKey(root, env);
  if (!key || !token) return null;
  const cached = readCache(key);
  if (cached) return cached;
  const opts = { env, token, timeoutMs: HELPER_LOOKUP_MS, consumer: "claude-code-mcp-headers" };
  if (post) opts.post = post;
  return resolveIndexedRepo(root, opts);
}

/** The token of a first-session sign-up, "" when none happened. */
export async function signedUpToken(opts = {}) {
  const r = await ensureCredential({
    hasToken: false,
    client: hookClient(),
    serverUrl: resolveServerUrl(),
    ...opts,
  });
  return r.status === "created" ? r.token : "";
}

/** Bound for waiting on the SessionStart hook's first-session credential
 *  (LOOM_MCP_HOOK_WAIT_MS overrides). */
export const HOOK_TOKEN_WAIT_MS = 5000;

/** Wait for SessionStart to materialize an install token before first-session
 *  sign-up. The helper cannot read token options from its environment. */
export async function hookToken({
  env = process.env,
  waitMs = envNumber("LOOM_MCP_HOOK_WAIT_MS", HOOK_TOKEN_WAIT_MS, { min: 0 }),
  pollMs = 200,
  read = () => readConfigFile(loomConfigPath(env)),
} = {}) {
  if (agentMode(env)) return ""; // an agent never takes the person's saved token
  const deadline = Date.now() + waitMs;
  for (;;) {
    const cfg = read();
    const token = String(cfg.token || "").trim();
    if (token) {
      if (cfg.url && !env.LOOM_BASE_URL) env.LOOM_BASE_URL = String(cfg.url);
      return token;
    }
    if (Date.now() >= deadline) return "";
    await new Promise((r) => setTimeout(r, pollMs));
  }
}

async function main() {
  let token = "";
  let out = {};
  try {
    resolveServerUrl();
    const fixed = Boolean(pluginDefaults().server_url);
    const knowsServer = Boolean(process.env.LOOM_BASE_URL || readConfigFile(loomConfigPath()).url);
    token = resolveToken() || (!fixed || !knowsServer ? await hookToken() : "") || (await signedUpToken());
    const dir = pluginDefaults().server_url ? process.cwd() : await sessionProject();
    out = headersFor(token, await checkoutScope(dir, { token }));
  } catch {
    out = headersFor(token);
  }
  process.stdout.write(JSON.stringify(out) + "\n");
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") main();
