// Pure helpers for the loom-run hooks, plus the per-session stash of
// recalled keys.
//
// One session is one Loom run. The first prompt opens it (goal = that
// prompt) and SessionEnd closes it. The hooks can say what the run set out
// to do, when it ended and what the agent said last; they never say which
// memories the agent relied on (memories_used stays the agent's own report
// through run.complete): a guess there would poison the signal the ledger
// exists to clean.

import os from "node:os";
import path from "node:path";

import { contentText, hookClient, redactSecrets, runPrefixFor } from "./loom-client.mjs";
import { readPrivateFile, writeJsonFile } from "./tmp-state.mjs";

const GOAL_MAX = 2000;
const REQUEST_MAX = 8000;
const FINAL_MAX = 4000;

/** Stable run id for a session: the ledger key every other hook (capture's
 *  observe.record, the primer) derives without state. */
export function runIdFor(sessionId, client = hookClient()) {
  const s = String(sessionId || "").trim();
  return s ? `${runPrefixFor(client)}-${s}` : "";
}

/** Body for run.begin from the first UserPromptSubmit of a session. */
export function beginBody(scope, payload, prompt) {
  const text = redactSecrets(String(prompt || "")).trim();
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    session_id: String(payload.session_id || "-"),
    run_id: runIdFor(payload.session_id),
    goal: text.slice(0, GOAL_MAX) || "(empty prompt)",
    user_request: text.slice(0, REQUEST_MAX),
    environment: hookClient(),
    consumer: hookClient(),
    metadata: {
      project: scope.project,
      ...(scope.repo ? { repo_id: scope.repo.id, repo_slug: scope.repo.slug } : {}),
      cwd: String(payload.cwd || ""),
      tags: scope.tags || [],
    },
  };
}

/** Claude Code SessionEnd `reason` → run status. A cleared or exited session
 *  finished as far as the ledger can tell; nothing here knows whether the
 *  task succeeded, so `outcome: "none"` keeps the hook from crediting the
 *  memories the agent may have named through the MCP tool. */
export function statusFor(reason) {
  const r = String(reason || "").toLowerCase();
  if (r === "error" || r === "crash") return "failed";
  return "completed";
}

const RETAIN_MODES = new Set(["keep", "demote", "expire"]);

/** What run.complete does to the rows the session wrote.
 *  Unset = the server default (`keep`); anything else is passed through. */
export function retainFor(env = process.env) {
  const v = String(env.LOOM_PLUGIN_RUN_RETAIN || "")
    .trim()
    .toLowerCase();
  return RETAIN_MODES.has(v) ? v : "";
}

/** Body for run.complete at SessionEnd. */
export function completeBody(scope, payload, finalText, env = process.env, recalledKeys = []) {
  const body = {
    org: scope.org,
    namespace: scope.namespace,
    run_id: runIdFor(payload.session_id),
    status: statusFor(payload.reason),
    final_response: redactSecrets(finalText).slice(0, FINAL_MAX),
    outcome: "none",
    metadata: { session_end_reason: String(payload.reason || "") },
  };
  const retain = retainFor(env);
  if (retain) body.retain = retain;
  // What the session's Loom tools SERVED — never
  // memories_used, which is the agent's own reliance label.
  const recalled = [...new Set((recalledKeys || []).map((k) => String(k || "").trim()).filter(Boolean))];
  if (recalled.length) body.recalled_keys = recalled.slice(0, 500);
  return body;
}

// --- recalled-key stash ---------------------------------
// The PostToolUse capture hook records the memory keys a loom_search /
// loom_memory_get result served; the SessionEnd run hook hands them to
// run.complete(recalled_keys). One file per session in $TMPDIR, best-effort.
const RECALLED_MAX = 200;

export function recalledStashPath(sessionId, tmp = os.tmpdir()) {
  const safe = String(sessionId || "nosession").replace(/[^\w-]/g, "_");
  return path.join(tmp, `loom-recalled-${safe}.json`);
}

export function readRecalledStash(sessionId, tmp = os.tmpdir()) {
  try {
    const rows = JSON.parse(readPrivateFile(recalledStashPath(sessionId, tmp)));
    return Array.isArray(rows) ? rows.map(String).filter(Boolean) : [];
  } catch {
    return [];
  }
}

export function appendRecalledStash(sessionId, keys, tmp = os.tmpdir()) {
  const merged = [...new Set([...readRecalledStash(sessionId, tmp), ...(keys || []).map(String).filter(Boolean)])];
  writeJsonFile(recalledStashPath(sessionId, tmp), merged.slice(-RECALLED_MAX)); // best-effort
  return merged.length;
}

/** The memory keys a Loom tool result served, whatever the envelope: a
 *  loom_search structured payload (`memory.results[].memory_key`), a
 *  memory.get result (`memory_key` when found), or a bare results list. */
export function servedMemoryKeys(body) {
  const out = [];
  const take = (rows) => {
    if (!Array.isArray(rows)) return;
    for (const r of rows) {
      const k = r && typeof r === "object" ? r.memory_key : null;
      if (typeof k === "string" && k && !k.startsWith("chunk:")) out.push(k);
    }
  };
  if (!body || typeof body !== "object") return out;
  const b = body.structuredContent && typeof body.structuredContent === "object" ? body.structuredContent : body;
  take(b.memory && b.memory.results);
  take(b.results);
  if (b.found && typeof b.memory_key === "string" && b.memory_key) out.push(b.memory_key);
  return [...new Set(out)];
}

/** The last assistant text block in transcript JSONL objects (sidechains
 *  skipped); "" when none. */
export function lastAssistantText(objs) {
  let last = "";
  for (const o of objs || []) {
    if (!o || o.type !== "assistant" || o.isSidechain) continue;
    const content = o.message && o.message.content;
    if (typeof content === "string") {
      if (content.trim()) last = content.trim();
      continue;
    }
    if (!Array.isArray(content)) continue;
    const text = contentText(content).trim();
    if (text) last = text;
  }
  return last;
}
