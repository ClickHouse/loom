#!/usr/bin/env node
// A plugin-only client's status check. No Loom checkout, Python or SDK needed.
import { readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  agentMode,
  decodeTokenClaims,
  loomConfig,
  loomConfigPath,
  loomTokenPath,
  normalizeUrl,
  requestJson,
  resolveScope,
  resolveServerUrl,
  tokenLookup,
} from "../lib/loom-client.mjs";
import { entryIsFixed, sameServer, statusServerUrl } from "../lib/plugin-options.mjs";

const FLUSH_RE = /^loom-flush-\d{13}-\d+\.json$/;
const WRITER_ROLES = new Set(["writer", "admin", "jwt", "org_admin", "platform_admin"]);
/** What the `source` line calls a `tokenLookup` kind. */
function tokenSourceName(kind) {
  if (kind === "config") return loomConfigPath();
  if (kind === "file") return loomTokenPath();
  return { env: "LOOM_API_KEY", agent: "the workload-token exchange", option: "the plugin's install option" }[kind];
}

function staleFlushCount(directory, now) {
  try {
    return readdirSync(directory).filter((name) => {
      if (!FLUSH_RE.test(name)) return false;
      try {
        return now - statSync(join(directory, name)).mtimeMs > 600_000;
      } catch {
        return false;
      }
    }).length;
  } catch {
    return 0;
  }
}

export async function clientStatus({
  serverUrl,
  token,
  scope,
  tokenKind = "",
  configUrl = "",
  // The server an agent's exchange went to: the hooks' server, which may
  // differ from the MCP entry's checked here.
  exchangeUrl = serverUrl,
  fetchImpl = fetch,
  now = Date.now(),
  tempDirectory = tmpdir(),
} = {}) {
  const checks = [];
  const add = (name, grade, detail) => checks.push({ name, grade, detail });
  const health = await requestJson(`${serverUrl}/v1/health`, { method: "GET", timeoutMs: 5000, fetchImpl });
  if (health.status === 200) {
    add("server", "PASS", `${serverUrl} answers; build ${health.body?.build_commit || "unknown"}`);
  } else {
    add(
      "server",
      "FAIL",
      health.status
        ? `${serverUrl} answered HTTP ${health.status}`
        : `cannot reach ${serverUrl}: ${health.error || "network error"}`,
    );
  }

  const claims = decodeTokenClaims(token);
  const validJwt = Boolean(token && token.split(".").length === 3 && Object.keys(claims).length);
  if (!token && agentMode()) {
    add(
      "token",
      "FAIL",
      `no Loom token: the workload-token exchange at ${exchangeUrl} failed (agent mode, LOOM_CLIENT_ID)`,
    );
  } else if (!validJwt) {
    add("token", "FAIL", token ? "token is not a readable JWT" : "no Loom token resolved");
  } else if (typeof claims.exp === "number" && claims.exp * 1000 <= now) {
    add("token", "FAIL", "token has expired");
  } else {
    add("token", "PASS", `${claims.sub || "unknown subject"} @ ${claims.org || "unknown org"}`);
  }
  if (tokenKind) {
    // A config token saved for another server is what the MCP header helper
    // sends to this one; the hooks may hold a different token.
    const source = tokenSourceName(tokenKind);
    const saved = normalizeUrl(configUrl);
    const other = tokenKind === "config" && saved && !sameServer(saved, serverUrl);
    add(
      "source",
      other ? "WARN" : "PASS",
      other ? `token from ${source}, which names ${saved}; this check uses ${serverUrl}` : `token from ${source}`,
    );
  }

  if (health.status === 200 && checks.find((c) => c.name === "token")?.grade === "PASS") {
    const auth = await requestJson(`${serverUrl}/v1/namespace.list_mine`, {
      body: { org: scope.org },
      token,
      timeoutMs: 10000,
      fetchImpl,
    });
    if (auth.status === 200 && Array.isArray(auth.body?.rows)) {
      const rows = auth.body.rows;
      add("auth", "PASS", `token accepted; ${rows.length} namespace(s) listed`);
      const row = rows.find((item) => item.namespace === scope.namespace && item.status !== "pending");
      if (row && WRITER_ROLES.has(row.role)) {
        add("namespace", "PASS", `${scope.namespace} has ${row.role} grant for hook writes`);
      } else {
        add("namespace", "FAIL", `${scope.namespace} has no listed writer grant`);
      }
    } else if (auth.status === 401 || auth.status === 403) {
      add("auth", "FAIL", `server rejected token (HTTP ${auth.status})`);
    } else {
      add(
        "auth",
        "WARN",
        auth.status
          ? `namespace.list_mine answered HTTP ${auth.status}`
          : `namespace.list_mine unreachable: ${auth.error || "network error"}`,
      );
    }
  } else {
    add("auth", "WARN", "skipped until server and token pass");
  }

  const stranded = staleFlushCount(tempDirectory, now);
  add(
    "flush",
    stranded ? "WARN" : "PASS",
    stranded ? `${stranded} capture job(s) older than ten minutes in ${tempDirectory}` : "no stale capture jobs",
  );
  const required = ["server", "token", "auth", "namespace"];
  return {
    ok: required.every((name) => checks.some((check) => check.name === name && check.grade === "PASS")),
    checks,
  };
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") {
  const { token, kind } = tokenLookup();
  const result = await clientStatus({
    serverUrl: statusServerUrl(),
    token,
    scope: resolveScope(process.cwd()),
    tokenKind: kind,
    // Under Codex the entry's URL is unknown here, so no comparison is made.
    configUrl: entryIsFixed() ? "" : loomConfig().url,
    exchangeUrl: resolveServerUrl(),
  });
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  if (!result.ok) process.exitCode = 1;
}
