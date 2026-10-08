#!/usr/bin/env node
// The MCP entry's headers helper. It authenticates from the same places the
// hooks do: LOOM_API_KEY, the install option, ~/.loom/config.json, then
// ~/.loom/token.
//
// It also names the checkout's code namespace. Claude Code runs the helper
// in the project directory; the checkout's resolved repository (the cache
// the shunt and the primer share, else one bounded lookup) becomes
// `X-Loom-Code-Scope: org/namespace`, which the server applies to every
// loom_code_* call that names no namespace. LOOM_MCP_CODE_SCOPE=off disables.
//
// No token → the helper signs this machine up through the
// same locked path the SessionStart hook uses, because
// Claude Code connects the MCP entry while that hook is still running. A
// second caller waits for the first one's token; one service account results.
// Sign-up off, refused or unreachable → `{}`: the server then answers 401 and
// Claude Code shows the connection as failed, which is the honest state.
// Stdout is exactly one JSON object; nothing else is written.
import { ensureCredential } from "../lib/first-session.mjs";
import { envSwitch, gitToplevel, hookClient, resolveServerUrl, resolveToken } from "../lib/loom-client.mjs";
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

async function main() {
  let token = "";
  let out = {};
  try {
    token = resolveToken() || (await signedUpToken());
    out = headersFor(token, await checkoutScope(process.cwd(), { token }));
  } catch {
    out = headersFor(token);
  }
  process.stdout.write(JSON.stringify(out) + "\n");
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") main();
