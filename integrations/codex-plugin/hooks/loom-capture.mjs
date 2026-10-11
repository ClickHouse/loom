#!/usr/bin/env node
// Loom ambient-capture hook — the WRITE side.
//
// One script, several lifecycle events: argv[2] names the event (hooks.json
// sets it), else the payload's `hook_event_name`. Every post is
// fire-and-forget (postFireAndForget) under the unref'd exit timer
// (scheduleExit); when in doubt the hook does nothing and exits 0.
//
// Events handled:
//   PostToolUse        → episodic observation of a successful tool action
//   PostToolUseFailure → episodic observation typed `error` (bug-triage gold)
//   PreCompact         → crystallize the session before context is dropped
//   Stop / SessionEnd  → crystallize the finished session
//
// Captures land via Loom's `observe.record` wire op (episodic plane,
// zero-token heuristic typing). Crystallization triggers `observe.crystallize`,
// which runs Loom's deferred extraction/embedding/conflict path server-side.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  commandText,
  envNumber,
  extractFiles,
  inferActionType,
  normalizeToolName,
  postFireAndForget,
  readStdinJson,
  redactSecrets,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
  truncate,
} from "../lib/loom-client.mjs";
import { appendRecalledStash, runIdFor, servedMemoryKeys } from "../lib/run-hook.mjs";
import { readJsonFile, removeFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const OBSERVE_OP = "observe.record";
const CRYSTALLIZE_OP = "observe.crystallize";

// A call to loom's own tools is not observed — its input IS a
// memory (observing loom_memory_set copies the value outside the key the
// user can delete), and the harness's meta tools say nothing about the task.
// The server applies the same rule (setting observe_skip_tools); skipping
// here saves the request. LOOM_OBSERVE_SKIP_TOOLS overrides
// the list (comma-separated names; a trailing * matches a prefix).
const DEFAULT_SKIP_TOOLS =
  "mcp__loom__*,loom_memory_*,loom_knowledge_*,loom_search,loom_observe*,loom_procedure_*," +
  "loom_context_*,loom_ask,ToolSearch,ListAgents,ListMcpResourcesTool,ReadMcpResourceTool," +
  "ReadMcpResourceDirTool,AskUserQuestion,TodoWrite,Skill,EnterPlanMode,ExitPlanMode," +
  "EnterWorktree,ExitWorktree";

// The sensory filter: a re-read of the same file (or the same grep) within a
// short window carries no new information, and every observation is one
// committed row (260–330 ms and one part per insert). Read-shaped actions
// repeated within LOOM_OBSERVE_DEDUPE_S seconds in one session are not posted
// again; edits, commands and errors always are. 0 disables.
export const DEDUPE_WINDOW_S = envNumber("LOOM_OBSERVE_DEDUPE_S", 60, { min: 0 });
const DEDUPED_ACTIONS = new Set(["file_read"]);

/** The identity of a read-shaped observation: tool + the files it named
 *  (or the command text); "" for anything that is never deduplicated. */
export function dedupeKey(observation) {
  if (!observation || !DEDUPED_ACTIONS.has(observation.action_type) || observation.error) return "";
  const files = Array.isArray(observation.file_paths) ? [...observation.file_paths].sort().join("|") : "";
  const what = files || commandText(parsedToolInput(observation.tool_input)).trim();
  return what ? `${observation.tool_name}:${what}` : "";
}

/** The tool input object behind an observation's (possibly truncated) JSON text. */
function parsedToolInput(text) {
  try {
    return JSON.parse(String(text ?? ""));
  } catch {
    return {};
  }
}

/** {suppress, state}: suppress when `key` was posted within `windowS` of
 *  `now`; the returned state records this posting (bounded to 200 keys). */
export function decideDedupe(state, key, now, windowS = DEDUPE_WINDOW_S) {
  const s = state && typeof state === "object" ? { ...state } : {};
  if (!key || windowS <= 0) return { suppress: false, state: s };
  const last = Number(s[key] || 0);
  if (last && now - last < windowS * 1000) return { suppress: true, state: s };
  s[key] = now;
  const keys = Object.keys(s);
  if (keys.length > 200) for (const k of keys.sort((a, b) => s[a] - s[b]).slice(0, keys.length - 200)) delete s[k];
  return { suppress: false, state: s };
}

export function shouldObserve(toolName, skipList = process.env.LOOM_OBSERVE_SKIP_TOOLS) {
  const name = String(toolName || "")
    .trim()
    .toLowerCase();
  if (!name) return true;
  const raw = typeof skipList === "string" && skipList.trim() !== "" ? skipList : DEFAULT_SKIP_TOOLS;
  for (const pat of raw
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean)) {
    if (pat.endsWith("*") ? name.startsWith(pat.slice(0, -1)) : name === pat) return false;
  }
  return true;
}

function isErrorEvent(event, payload) {
  if (event === "PostToolUseFailure") return true;
  // Claude Code may report failures on PostToolUse via a status flag.
  const r = payload.tool_response || {};
  if (r && typeof r === "object" && (r.is_error || r.isError || r.error)) return true;
  return false;
}

/** `value` as text with secrets redacted, then truncated. Each string is
 *  redacted before JSON escaping can hide a pattern's word boundary, and the
 *  JSON once more for `"password": "…"` pairs; redacting before the cut keeps
 *  a truncated key from slipping past its pattern. */
function scrubbed(value, max, token = resolveToken()) {
  const text =
    typeof value === "string"
      ? value
      : JSON.stringify(value ?? "", (_, v) => (typeof v === "string" ? redactSecrets(v, token) : v));
  return truncate(redactSecrets(text, token), max);
}

export function buildObservation(event, payload, scope) {
  const toolName = payload.tool_name || "unknown";
  const toolInput = payload.tool_input || {};
  const toolResponse = payload.tool_response || "";
  const isError = isErrorEvent(event, payload);
  const actionType = inferActionType(toolName, isError);
  const files = extractFiles(toolInput);
  const errText = isError
    ? (toolResponse && (toolResponse.error || toolResponse.stderr || toolResponse.message)) || toolResponse
    : "";
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    // Redacted on the client before it leaves the machine (best effort).
    tool_input: scrubbed(toolInput, 800),
    tool_output: scrubbed(toolResponse, 800),
    error: isError ? scrubbed(errText, 600) : null,
    file_paths: files,
    session_id: payload.session_id || "-",
    // The session's run (loom-run.mjs opens it on the first prompt), so the
    // observation joins the run ledger without the agent doing anything.
    run_id: runIdFor(payload.session_id) || "-",
    // Heuristic, zero-token. Server defers embedding/extraction.
    memory_type: "episodic",
    action_type: actionType,
    tool_name: toolName,
    tags: [...scope.tags, `action:${actionType}`],
    metadata: {
      project: scope.project,
      // The repository by its remote, not its folder, and
      // where in it the agent was working.
      ...(scope.repo ? { repo_id: scope.repo.id, repo_slug: scope.repo.slug } : {}),
      ...(scope.relDir ? { rel_dir: scope.relDir } : {}),
      event,
    },
    observed_at: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Code-plane outcome signals. When a loom_code_* MCP tool returns,
// stash its trace_run_id + returned paths in a per-session temp file; a later
// Read of a returned path (10-min window) reports `opened_file`, a later
// grep after a code search reports `fell_back_grep`. Free per-query relevance
// labels in loom.code_search_outcome. All fire-and-forget.
// ---------------------------------------------------------------------------

const CODE_STASH_TTL_MS = 10 * 60 * 1000;
const CODE_OUTCOME_OP = "code.outcome";
// The tools whose results serve memory keys.
const MEMORY_SERVED_TOOL_RE = /loom_(search|memory_get|memory_search)\b/;

// Every code op whose response carries a trace_run_id (each server read
// handler writes a loom.code_search_trace row).
const CODE_TRACED_TOOL_RE =
  /loom_code_(search|symbol|ask|trace|callers|callees|impact|verify|grep|context_pack|examples|history|map)\b/;

/** Repo paths a code op's response points at, whatever its shape. Exported
 *  for tests via handleCodeSignals' behaviour; kept tiny and total. */
export function codeResponsePaths(body) {
  const out = new Set();
  const take = (rows, key = "path") => {
    if (!Array.isArray(rows)) return;
    for (const r of rows) {
      const v = r && typeof r === "object" ? r[key] : r;
      if (typeof v === "string" && v) out.add(v);
    }
  };
  take(body.results); // search
  take(body.definitions); // symbol, trace
  take(body.citations); // ask
  take(body.edges); // callers / callees (call-site file)
  take(body.impacted_symbols); // impact
  take(body.touched_symbols);
  take(body.matches); // grep
  take(body.evidence); // verify
  take(body.frames); // trace (resolved frames)
  take(body.paths, null); // history (plain strings)
  return [...out].slice(0, 50);
}

/** Why a grep followed a code answer, from the server's closed vocabulary:
 *  the answer abstained, or it came back with no path to
 *  open, or the agent grepped despite an answer it could have used. The
 *  hook cannot know more than that; the server records anything else as
 *  "unattributed". Exported for tests. */
export function fallbackReason(row) {
  if (!row) return "unattributed";
  if (row.abstained) return "abstained";
  if (!Array.isArray(row.paths) || row.paths.length === 0) return "empty";
  return "preference";
}

function codeStashPath(sessionId) {
  const safe = String(sessionId || "nosession").replace(/[^\w-]/g, "_");
  return path.join(os.tmpdir(), `loom-code-trace-${safe}.json`);
}

function readCodeStash(sessionId) {
  try {
    const raw = fs.readFileSync(codeStashPath(sessionId), "utf8");
    const rows = JSON.parse(raw);
    const cutoff = Date.now() - CODE_STASH_TTL_MS;
    return Array.isArray(rows) ? rows.filter((r) => r.ts > cutoff) : [];
  } catch {
    return [];
  }
}

function writeCodeStash(sessionId, rows) {
  writeJsonFile(codeStashPath(sessionId), rows.slice(-20)); // best-effort
}

/** The span a Read covered, as CodeOutcomeInput.spans wants it: "start-end"
 *  from offset/limit (limit defaults to Claude Code's 2000, clipped to the
 *  file when it can be counted), "" for a whole-file read. Credit for an
 *  opened_file should land on the unit whose lines were read,
 *  not on every unit of the path. Exported for tests. */
export function readSpanOf(toolInput, countLines = countFileLines) {
  const offset = toolInput.offset;
  const limit = toolInput.limit;
  if (offset == null && limit == null) return "";
  const start = Math.max(1, Math.floor(Number(offset) || 1));
  const want = limit != null && Number(limit) > 0 ? Math.floor(Number(limit)) : 2000;
  const total = countLines(String(toolInput.file_path || ""));
  const end = total > 0 ? Math.min(total, start + want - 1) : start + want - 1;
  return end >= start ? `${start}-${end}` : "";
}

function countFileLines(abs) {
  try {
    const st = fs.statSync(abs);
    if (!st.isFile() || st.size > 4 * 1024 * 1024) return 0;
    const buf = fs.readFileSync(abs);
    let n = 0;
    for (let i = 0; i < buf.length; i++) if (buf[i] === 10) n++;
    return n + (buf.length && buf[buf.length - 1] !== 10 ? 1 : 0);
  } catch {
    return 0;
  }
}

function handleCodeSignals(payload, scope, opts) {
  const sessionId = payload.session_id || "-";
  const toolName = normalizeToolName(payload.tool_name || "");
  const toolInput = payload.tool_input || {};
  const resp = payload.tool_response || {};

  // 1) a loom_code_* tool returned -> stash its trace + paths
  if (CODE_TRACED_TOOL_RE.test(toolName)) {
    const body = resp && typeof resp === "object" ? resp.structuredContent || resp : {};
    const traceId = body.trace_run_id || "";
    const abstained = body.abstain === true || body.verdict === "not_established";
    const paths = codeResponsePaths(body);
    if (traceId) {
      const rows = readCodeStash(sessionId);
      rows.push({ ts: Date.now(), traceId, paths, abstained });
      writeCodeStash(sessionId, rows);
    }
    return;
  }

  const stash = readCodeStash(sessionId);
  if (!stash.length) return;

  // 2) Read of a returned path -> opened_file
  if (toolName === "Read" && typeof toolInput.file_path === "string") {
    const opened = toolInput.file_path;
    for (const row of stash) {
      if (row.paths.some((p) => opened.endsWith(p) || p.endsWith(opened))) {
        postFireAndForget(
          CODE_OUTCOME_OP,
          {
            trace_run_id: row.traceId,
            action: "opened_file",
            targets: [opened],
            spans: [readSpanOf(toolInput)],
            source: "plugin_hook",
          },
          opts,
        );
        return;
      }
    }
    return;
  }

  // 3) grep right after a code search -> fell_back_grep on the newest trace
  const cmd = commandText(toolInput);
  const isGrep = toolName === "Grep" || /(^|[\s|;])(rg|grep)\s/.test(cmd);
  if (isGrep) {
    const latest = stash[stash.length - 1];
    postFireAndForget(
      CODE_OUTCOME_OP,
      {
        trace_run_id: latest.traceId,
        action: "fell_back_grep",
        targets: [],
        source: "plugin_hook",
        reason: fallbackReason(latest),
      },
      opts,
    );
  }
}

async function main() {
  const event = (process.argv[2] || "").trim();
  const payload = await readStdinJson();
  const resolvedEvent = event || payload.hook_event_name || "PostToolUse";
  if (resolvedEvent === "SessionEnd") removeFile(codeStashPath(payload.session_id));

  const token = resolveToken();
  const serverUrl = resolveServerUrl();
  // No creds → do nothing, fast. Don't block the agent on a misconfig.
  if (!token) {
    scheduleExit(0);
    return;
  }

  const scope = resolveScope(payload.cwd);
  const opts = { token, serverUrl };

  if (resolvedEvent === "PostToolUse" || resolvedEvent === "PostToolUseFailure") {
    try {
      handleCodeSignals(payload, scope, opts);
    } catch {
      /* outcome telemetry must never block capture */
    }
    try {
      // Remember what a Loom memory tool served, for the
      // run hook's run.complete(recalled_keys) at SessionEnd.
      const tool = normalizeToolName(payload.tool_name || "");
      if (MEMORY_SERVED_TOOL_RE.test(tool)) {
        const keys = servedMemoryKeys(payload.tool_response || {});
        if (keys.length) appendRecalledStash(payload.session_id || "-", keys);
      }
    } catch {
      /* the stash is best-effort */
    }
  }

  if (resolvedEvent === "PreCompact" || resolvedEvent === "Stop" || resolvedEvent === "SessionEnd") {
    // Crystallize: ask the server to run its deferred extraction over the
    // session's episodic rows. Fire-and-forget — we do not wait for the
    // (potentially LLM-heavy) work to finish.
    postFireAndForget(
      CRYSTALLIZE_OP,
      {
        org: scope.org,
        namespace: scope.namespace,
        agent: scope.agent,
        session_id: payload.session_id || "-",
        reason: resolvedEvent === "PreCompact" ? "pre_compact" : "session_end",
        tags: scope.tags,
      },
      opts,
    );
  } else if (shouldObserve(payload.tool_name)) {
    // PostToolUse / PostToolUseFailure → record one episodic observation.
    // (A loom tool call or a meta tool is not one.)
    const observation = buildObservation(resolvedEvent, payload, scope);
    const key = dedupeKey(observation);
    let post = true;
    if (key) {
      const dp = sessionStatePath("capture-seen", payload.session_id);
      const d = decideDedupe(readJsonFile(dp), key, Date.now());
      post = !d.suppress;
      writeJsonFile(dp, d.state);
    }
    if (post) postFireAndForget(OBSERVE_OP, observation, opts);
  }

  // Hard, unref'd exit. fetch is detached; this guarantees we never hold
  // the prompt boundary open past the grace window.
  scheduleExit(0);
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") runHookMain(main);
