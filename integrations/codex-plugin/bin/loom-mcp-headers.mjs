#!/usr/bin/env node
// The MCP entry's headers helper. It authenticates from the places the hooks
// do, less the install option, which is not in its environment: LOOM_API_KEY;
// for an agent (LOOM_CLIENT_ID with a workload token) its exchanged token and
// nothing else; otherwise ~/.loom/config.json, then ~/.loom/token.
//
// It also names the code namespace of the checkout it runs in. Claude Code
// starts a plugin's helper from the plugin root, so there the header does not
// name the project's checkout (LOOM_CODE_REPO / `code_repo` still name one).
// The resolved repository (the cache the shunt and the primer share, else one
// bounded lookup) becomes `X-Loom-Code-Scope: org/namespace`, which the server
// applies to every loom_code_* call that names no namespace.
// LOOM_MCP_CODE_SCOPE=off disables.
//
// No token and not an agent → the helper waits for the SessionStart hook,
// which Claude Code runs while it connects the MCP entry and which knows the
// configured server, to save the first-session credential; only then does it
// sign up itself, through the same locked path, so one account results.
// Sign-up off, refused or unreachable → `{}`: the server then answers 401
// and Claude Code shows the connection as failed, which is the honest state.
// Stdout is exactly one JSON object; nothing else is written.
import { ensureCredential } from "../lib/first-session.mjs";
import {
  agentMode,
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

/** The session's indexed repository for `cwd`: the shared cache, else one
 *  bounded lookup (which also fills the cache). Outside a checkout the
 *  repository named by LOOM_CODE_REPO / `code_repo` stands in; null when
 *  there is neither, without a token, or when disabled. */
export async function checkoutScope(cwd = process.cwd(), { env = process.env, token = "", post } = {}) {
  if (!envSwitch("LOOM_MCP_CODE_SCOPE", true, env)) return null;
  const root = gitToplevel(cwd);
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

/** The token the SessionStart hook saves to ~/.loom/config.json. The helper
 *  cannot see install options (the server URL), so on a first session it
 *  waits for the hook instead of signing up against a guessed server, and
 *  adopts the URL the hook saved with the token. */
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
    // Without LOOM_BASE_URL or a saved URL the configured server is unknown here.
    const knowsServer = Boolean(process.env.LOOM_BASE_URL || readConfigFile(loomConfigPath()).url);
    token = resolveToken() || (knowsServer ? "" : await hookToken()) || (await signedUpToken());
    out = headersFor(token, await checkoutScope(process.cwd(), { token }));
  } catch {
    out = headersFor(token);
  }
  process.stdout.write(JSON.stringify(out) + "\n");
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") main();
