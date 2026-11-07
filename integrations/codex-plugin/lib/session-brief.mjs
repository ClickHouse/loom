// Pure helpers for the session brief hook (unit-tested, no I/O).
//
// The working-memory note says where the last session stopped:
// its first prompt, the tree, the files it edited, its last reply. What the
// next session needs to carry the work on is what the note cannot hold: why
// the current approach was chosen, what was tried and dropped, what is still
// open, what comes next. That lives in the transcript — every prompt, reply,
// tool call and tool result — which is exactly what compaction throws away.
//
// This module turns a transcript (Claude Code JSONL, or a Codex rollout) into
// typed events, one per thing that happened: a user ask, a reply, an edit, a
// command with its one-line description, an error with its first lines, a
// read, a todo list, an identifier a command printed (a PR URL, a commit).
// Zero model tokens: the server condenses the events under a budget and
// revises the brief (session.brief_save). The hook sends only the events
// after its cursor, so each save carries one window of the session.

import {
  commandText,
  contentText,
  envSwitch,
  extractFiles,
  normalizeToolName,
  redactHead,
  resolveToken,
} from "./loom-client.mjs";
import { recalledDataBlock } from "./prompt-recall.mjs";

export const MAX_EVENTS = 3000;
export const TEXT_MAX = 1500;
// The client's compaction summary: the session model wrote
// it with every tool result in view — the best evidence a window carries.
export const SUMMARY_MAX = 15000;
const SUMMARY_PREAMBLE = /^This session is being continued from a previous conversation[^\n]*\n+(?:Summary:\s*)?/;
const ERROR_MAX = 600;
const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "str_replace_editor", "apply_patch"]);
const READ_TOOLS = new Set(["Read", "LS", "NotebookRead"]);
const SEARCH_TOOLS = new Set(["Grep", "Glob", "ToolSearch"]);

/** The SessionStart line naming saved briefs. Off by default: resuming is
 *  something the person asks for, not something every session is nudged
 *  towards. LOOM_SESSION_BRIEF_PRIMER=on shows it. */
export function briefPrimerEnabled(env = process.env) {
  return briefEnabled(env) && envSwitch("LOOM_SESSION_BRIEF_PRIMER", false, env);
}

/** Off with LOOM_SESSION_BRIEF=off (or 0/false/no). */
export function briefEnabled(env = process.env) {
  return envSwitch("LOOM_SESSION_BRIEF", true, env);
}

let token; // looked up once per hook run, not per clipped string

// Every brief field passes through here, so it redacts before the cut.
function clip(text, max = TEXT_MAX) {
  token ??= resolveToken();
  const s = redactHead(text || "", max, token)
    .replace(/\s+/g, " ")
    .trim();
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

// Identifiers worth a pointer when a command prints them: a pull request,
// issue or commit URL; the summary line `git commit` prints ([branch sha]).
const POINTER_RES = [
  [/https?:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(?:pull|issues|commit)\/\w+/g, (m) => m[0]],
  [/^\[[\w./-]+ ([0-9a-f]{7,12})\] /gm, (m) => `commit ${m[1]}`],
];

/** Pointer-shaped tokens in a command's output (deduped, at most 6). */
export function outputPointers(text) {
  const s = String(text || "").slice(0, 20000);
  const out = [];
  for (const [re, fmt] of POINTER_RES) {
    for (const m of s.matchAll(re)) {
      const tok = fmt(m);
      if (!out.includes(tok)) out.push(tok);
      if (out.length >= 6) return out;
    }
  }
  return out;
}

function todoNote(input) {
  const todos = Array.isArray(input && input.todos) ? input.todos : [];
  const open = todos.filter((t) => t && t.status !== "completed");
  if (!todos.length) return "";
  const done = todos.length - open.length;
  const parts = open.slice(0, 8).map((t) => `[${t.status || "pending"}] ${clip(t.content || t.subject || "", 120)}`);
  return clip(`todos (${done}/${todos.length} done): ${parts.join("; ")}`, 400);
}

function toolEvent(name, input, ts, ref) {
  const n = normalizeToolName(name);
  const inp = input && typeof input === "object" ? input : {};
  if (EDIT_TOOLS.has(n)) {
    const files = extractFiles(inp);
    const verb = n === "Write" ? "wrote" : "edited";
    return files.map((p) => ({ kind: "edit", path: String(p).slice(0, 500), text: verb, ts, ref }));
  }
  if (n === "Bash") {
    const cmd = commandText(inp);
    const desc = typeof inp.description === "string" ? inp.description.trim() : "";
    const text = desc ? `${desc} — $ ${clip(cmd, 160)}` : `$ ${clip(cmd, 240)}`;
    return [{ kind: "command", text: clip(text, 400), ts, ref }];
  }
  if (READ_TOOLS.has(n)) {
    const files = extractFiles(inp);
    return [{ kind: "read", path: String(files[0] || "").slice(0, 500), text: "", ts, ref }];
  }
  if (SEARCH_TOOLS.has(n)) {
    const q = inp.pattern || inp.query || inp.glob || "";
    return [{ kind: "search", text: clip(`${n} ${q}${inp.path ? ` in ${inp.path}` : ""}`, 200), ts, ref }];
  }
  if (n === "TodoWrite") {
    const note = todoNote(inp);
    return note ? [{ kind: "note", text: note, ts, ref }] : [];
  }
  if (n === "Task" || n === "Agent") {
    return [{ kind: "tool", text: clip(`subagent: ${inp.description || inp.prompt || ""}`, 200), ts, ref }];
  }
  if (n === "WebFetch" || n === "WebSearch") {
    return [{ kind: "tool", text: clip(`${n} ${inp.url || inp.query || ""}`, 200), ts, ref }];
  }
  const short = n.replace(/^mcp__[^_]+__/, "");
  return [{ kind: "tool", text: clip(short, 120), ts, ref }];
}

function resultText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return contentText(content);
  return "";
}

/** Events from Claude Code transcript objects (sidechains skipped). */
function claudeEvents(o, calls, out) {
  // Sidechains are subagents; isMeta rows are text the client injected (a
  // skill body, a caveat) in a user row. Neither is the person speaking.
  if (o.isSidechain || o.isMeta || !o.message) return;
  if (o.isCompactSummary) {
    const text = contentText(o.message.content).replace(SUMMARY_PREAMBLE, "").trim();
    if (text)
      out.push({
        kind: "summary",
        text: clip(text, SUMMARY_MAX),
        ts: String(o.timestamp || "").slice(0, 40),
        ref: String(o.uuid || "").slice(0, 120),
      });
    return;
  }
  const ts = String(o.timestamp || "").slice(0, 40);
  const ref = String(o.uuid || "").slice(0, 120);
  const content = o.message.content;
  if (o.type === "user") {
    if (Array.isArray(content) && content.some((b) => b && b.type === "tool_result")) {
      for (const b of content) {
        if (!b || b.type !== "tool_result") continue;
        const call = calls.get(b.tool_use_id) || {};
        const text = resultText(b.content);
        if (b.is_error) {
          const where = call.path ? ` [${call.path}]` : "";
          out.push({ kind: "error", text: clip(`${call.name || "tool"}: ${text}`, ERROR_MAX) + where, ts, ref });
        } else if (call.name === "Bash") {
          const ptrs = outputPointers(text);
          if (ptrs.length) out.push({ kind: "note", text: `output: ${ptrs.join(" ")}`, ts, ref });
        }
      }
      return;
    }
    const text = contentText(content).trim();
    if (!text || text.startsWith("<")) return; // injected/system/command rows
    out.push({ kind: "user", text: clip(text), ts, ref });
    return;
  }
  if (o.type !== "assistant" || !Array.isArray(content)) return;
  const said = content
    .filter((b) => b && b.type === "text")
    .map((b) => b.text)
    .join("\n")
    .trim();
  if (said) out.push({ kind: "assistant", text: clip(said), ts, ref });
  for (const b of content) {
    if (!b || b.type !== "tool_use") continue;
    const name = normalizeToolName(b.name);
    const files = extractFiles(b.input);
    calls.set(b.id, { name, path: files[0] || "" });
    out.push(...toolEvent(b.name, b.input, ts, ref));
  }
}

function codexText(content) {
  if (!Array.isArray(content)) return "";
  return content
    .map((c) => (c && typeof c.text === "string" ? c.text : ""))
    .join("\n")
    .trim();
}

/** Events from Codex rollout objects (response_item records). */
function codexEvents(o, calls, out) {
  const p = o.payload || {};
  const ts = String(o.timestamp || "").slice(0, 40);
  const ref = String(p.call_id || p.id || "").slice(0, 120);
  if (p.type === "message") {
    const text = codexText(p.content);
    if (!text || text.startsWith("<")) return;
    if (p.role === "user") out.push({ kind: "user", text: clip(text), ts, ref });
    else if (p.role === "assistant") out.push({ kind: "assistant", text: clip(text), ts, ref });
    return;
  }
  if (p.type === "function_call" || p.type === "custom_tool_call") {
    let input = p.input !== undefined ? p.input : p.arguments;
    if (typeof input === "string" && p.type === "function_call") {
      try {
        input = JSON.parse(input);
      } catch {
        /* keep the string */
      }
    }
    const name = String(p.name || "");
    if (name === "apply_patch" || (typeof input === "string" && input.includes("*** Begin Patch"))) {
      const files = extractFiles({ input: typeof input === "string" ? input : "" });
      calls.set(p.call_id, { name: "apply_patch", path: files[0] || "" });
      for (const f of files) out.push({ kind: "edit", path: String(f).slice(0, 500), text: "edited", ts, ref });
      return;
    }
    const obj = typeof input === "object" && input ? input : { command: String(input || "") };
    calls.set(p.call_id, { name: normalizeToolName(name) });
    out.push(...toolEvent(["exec", "exec_command", "shell"].includes(name) ? "Bash" : name, obj, ts, ref));
    return;
  }
  if (p.type === "function_call_output" || p.type === "custom_tool_call_output") {
    const call = calls.get(p.call_id) || {};
    const text = typeof p.output === "string" ? p.output : codexText(p.output);
    if (/\b(exit code|exited with) [1-9]\d*\b|\bTraceback \(most recent|\bError:/i.test(text.slice(0, 4000))) {
      out.push({ kind: "error", text: clip(`${call.name || "tool"}: ${text}`, ERROR_MAX), ts, ref });
    } else {
      const ptrs = outputPointers(text);
      if (ptrs.length) out.push({ kind: "note", text: `output: ${ptrs.join(" ")}`, ts, ref });
    }
  }
}

/** Typed events from parsed transcript objects, in order, bounded. */
export function transcriptEvents(objs, { max = MAX_EVENTS } = {}) {
  const out = [];
  const calls = new Map();
  for (const o of objs || []) {
    if (!o || typeof o !== "object") continue;
    if (o.type === "response_item") codexEvents(o, calls, out);
    else if (o.type === "user" || o.type === "assistant") claudeEvents(o, calls, out);
  }
  // A window larger than the server takes keeps its head and its tail; the
  // middle of a very long window matters least (the server condenses anyway).
  if (out.length <= max) return out;
  const head = Math.floor(max / 3);
  return [
    ...out.slice(0, head),
    { kind: "note", text: `… ${out.length - max} events of this window omitted by the client` },
    ...out.slice(out.length - (max - head - 1)),
  ];
}

/** Events from the turn journal (a client that gives no parseable
 *  transcript): the prompts and replies the hooks saw. */
export function journalEvents(rows) {
  const out = [];
  for (const r of rows || []) {
    const text = clip(r && r.content);
    if (!text) continue;
    out.push({ kind: r.role === "assistant" ? "assistant" : "user", text, ts: String(r.at || "").slice(0, 40) });
  }
  return out;
}

/** Is this window worth a brief? A first save needs a person's ask and some
 *  work behind it (an edit, or a dozen events) — a one-question session
 *  leaves the bookmark, not a brief (a tiny session yields only trivia).
 *  A later save on an existing brief needs any user or edit event,
 *  or a handful of events. */
export function worthSaving(events, { firstSave = true } = {}) {
  const evs = events || [];
  const users = evs.filter((e) => e.kind === "user").length;
  const edits = evs.filter((e) => e.kind === "edit").length;
  if (firstSave) return users >= 1 && (edits >= 1 || evs.length >= 12);
  return users + edits >= 1 || evs.length >= 5;
}

/** The session.brief_save body. brief_key is left to the server
 *  (auto/<project>/<branch>) unless LOOM_SESSION_BRIEF_KEY names one. */
export function briefBody(scope, { sessionId, branch, head, trigger, events, cursor, repo, env = process.env }) {
  const key = String(env.LOOM_SESSION_BRIEF_KEY || "").trim();
  const body = {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    session_id: sessionId,
    project: String(scope.project || "").slice(0, 120),
    repo: String(repo || (scope.repo && scope.repo.slug) || "").slice(0, 300),
    branch: String(branch || "").slice(0, 200),
    head_sha: String(head || "").slice(0, 64),
    trigger,
    events,
    event_cursor: String(cursor),
  };
  if (key) body.brief_key = key;
  return body;
}

/** Pointer for the SessionStart primer: the briefs this project has, so the
 *  session knows it can resume without reading them all. In a shared namespace
 *  the list holds other members' briefs, so the keys and goals go inside a
 *  recalled-data block. */
export function primerLine(briefs, { max = 3 } = {}) {
  const items = (briefs || []).slice(0, max);
  if (!items.length) return "";
  const rows = items.map((b) => {
    const age = ago(Number(b.age_seconds) || 0);
    const goal = clip(b.goal || b.status || "", 90);
    return `${b.brief_key} (rev ${b.revision}, ${age}${goal ? `: ${goal}` : ""})`;
  });
  return (
    `Saved session briefs for this project:\n${recalledDataBlock(rows.join("\n"))}\n` +
    "To resume one, run the loom-session skill (or session.brief_get) — a brief is where earlier work stood, not a source of facts."
  );
}

export function ago(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

/** The server's default brief address for (project, branch) —
 *  `server.memory.domain.session_brief.auto_brief_key`, mirrored so a client can
 *  name the brief it would revise without a round trip (a test holds the two
 *  to the same outputs). */
export function autoBriefKey(project, branch) {
  const part = (s, n) =>
    String(s || "")
      .trim()
      .replace(/[^A-Za-z0-9_.+-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, n);
  const p = part(project, 60) || "project";
  const b = part(branch ? String(branch).replace(/\//g, "-") : "", 80) || "detached";
  return `auto/${p}/${b}`;
}
