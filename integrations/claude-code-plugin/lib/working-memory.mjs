// Pure helpers for the loom-working-memory hook (unit-tested; no I/O).
//
// Working memory is the quadrant nothing else writes: the server has
// `working_memory.*` and `context.pack` loads the blocks ahead of every
// durable section, but without this hook a coding session produces none.
//
// The hook writes ONE block per project at Stop and PreCompact: what the
// session set out to do, which files it has touched, what the working tree
// looks like, and what the agent last said. It is the "where was I" note a
// person keeps on a sticky; `context.pack` puts it first in the primer that
// SessionStart injects, and Claude Code fires SessionStart again after a
// compaction, so the note survives the context drop it was written before.
//
// It is state, not knowledge: durable facts still go through memory.set,
// and this block is never embedded or ranked: working memory is not
// retrieval memory.

import { isAbsolute } from "node:path";
import { contentText, pathUnder, redactHead, resolveToken } from "./loom-client.mjs";

export const BLOCK_CHAR_LIMIT = 3000;
const GOAL_MAX = 300;
const LAST_MAX = 500;
const FILES_MAX = 12;
const STATUS_MAX = 10;

/** Block key: one note per project, so two repos in one namespace never
 *  overwrite each other's state. */
export function blockKeyFor(project) {
  const p = String(project || "")
    .trim()
    .replace(/[^\w.-]/g, "_")
    .slice(0, 60);
  return `session:${p || "project"}`;
}

/** First user prompt in transcript JSONL objects (sidechains skipped); "". */
export function firstUserPrompt(objs) {
  for (const o of objs || []) {
    if (!o || o.type !== "user" || o.isSidechain) continue;
    const content = o.message && o.message.content;
    const text = contentText(content).trim();
    // Tool results also arrive as `user` rows; skip those and empty rows.
    if (!text || (Array.isArray(content) && content.some((b) => b && b.type === "tool_result"))) continue;
    return text;
  }
  return "";
}

const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"]);

/** Files the session edited, in first-touch order, deduped. Read from the
 *  assistant tool_use blocks in the transcript (no server round-trip). */
export function editedFiles(objs) {
  const seen = new Set();
  const out = [];
  for (const o of objs || []) {
    if (!o || o.type !== "assistant" || o.isSidechain) continue;
    const content = o.message && o.message.content;
    if (!Array.isArray(content)) continue;
    for (const b of content) {
      if (!b || b.type !== "tool_use" || !EDIT_TOOLS.has(String(b.name || ""))) continue;
      const p = b.input && (b.input.file_path || b.input.notebook_path || b.input.path);
      if (typeof p !== "string" || !p || seen.has(p)) continue;
      seen.add(p);
      out.push(p);
    }
  }
  return out;
}

/** `git status --short` lines → { branch, changed: ["M a.py", ...] }. The
 *  caller passes the raw text of `git status --short --branch`. */
export function parseGitStatus(text) {
  const lines = String(text || "")
    .split("\n")
    .filter((l) => l.trim());
  let branch = "";
  const changed = [];
  for (const l of lines) {
    if (l.startsWith("## ")) {
      branch = l.slice(3).split("...")[0].trim();
      continue;
    }
    changed.push(l.trimEnd());
  }
  return { branch, changed };
}

/** An absolute `p` relative to `cwd` when it lies under it (either slash, any
 *  spelling of the directory); anything else as given. */
function short(p, cwd) {
  return (cwd && isAbsolute(String(p)) && pathUnder(String(cwd), p)) || p;
}

/** The block body. Deterministic for the same inputs (the hook dedupes on
 *  a hash of it), bounded to BLOCK_CHAR_LIMIT, plain text the model reads. */
/** The line under the title: what this note is and is not. */
export const BOOKMARK_FRAME =
  "(a bookmark of where the previous session stopped — goal, tree, files, last reply. " +
  "Not a source of facts: answer what/where/who-do-I questions with loom_search, not from here.)";

export function buildSessionState({ goal, files, git, lastText, cwd, writtenAt, reason }) {
  const lines = [];
  const when = writtenAt || new Date().toISOString();
  // The note is a bookmark of where the previous session
  // stopped, and the next session's primer loads it first. Without the frame
  // a model may answer a fact question from the previous turn's reply in
  // this note, without a search, after the fact was edited.
  lines.push(`# Session state — written by the ${reason || "Stop"} hook at ${when}`);
  lines.push(BOOKMARK_FRAME);
  const token = resolveToken();
  const g = redactHead(goal, GOAL_MAX, token).trim();
  if (g) lines.push(`goal: ${g.length > GOAL_MAX ? g.slice(0, GOAL_MAX) + "…" : g}`);
  const st = git || { branch: "", changed: [] };
  if (st.branch || (st.changed && st.changed.length)) {
    const head = st.branch ? `branch: ${st.branch}` : "branch: (unknown)";
    const n = (st.changed || []).length;
    lines.push(n ? `${head} — ${n} uncommitted change${n === 1 ? "" : "s"}` : `${head} — clean tree`);
    for (const c of (st.changed || []).slice(0, STATUS_MAX)) lines.push(`  ${c}`);
    if (n > STATUS_MAX) lines.push(`  … ${n - STATUS_MAX} more`);
  }
  const fs = (files || []).map((p) => short(p, cwd));
  if (fs.length) {
    const shown = fs.slice(0, FILES_MAX).join(", ");
    lines.push(`edited this session: ${shown}${fs.length > FILES_MAX ? `, … ${fs.length - FILES_MAX} more` : ""}`);
  }
  const last = redactHead(lastText, LAST_MAX, token).replace(/\s+/g, " ").trim();
  if (last)
    lines.push(
      `last reply (the previous turn's words, not a fact): ${last.length > LAST_MAX ? last.slice(0, LAST_MAX) + "…" : last}`,
    );
  let out = lines.join("\n");
  if (out.length > BLOCK_CHAR_LIMIT) out = out.slice(0, BLOCK_CHAR_LIMIT - 1) + "…";
  return out;
}

/** Body for working_memory.set. Agent scope (session_id/run_id default to
 *  "-") so the SAME block is what context.pack loads at the next
 *  SessionStart — after a compaction, a resume, or a brand-new session on
 *  the same project. */
export function setBody(scope, project, content) {
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    block_key: blockKeyFor(project),
    content: String(content || "").slice(0, BLOCK_CHAR_LIMIT),
    char_limit: BLOCK_CHAR_LIMIT,
  };
}

/** Skip the POST when the note has not changed since the last write (Stop
 *  fires after every assistant turn). The hook compares the hash of the body
 *  WITHOUT its timestamp line, so a turn that changed nothing writes nothing. */
export function stateFingerprint(content) {
  return String(content || "")
    .split("\n")
    .slice(1)
    .join("\n");
}

// --- run scratchpad (opt-in LOOM_PLUGIN_SCRATCHPAD=1) --------
// An agent that re-reads a per-task scratchpad on every step does measurably
// better than one without it. The session note above is the "where was I"
// for the NEXT session; the scratchpad is the running log of what THIS run
// concluded, turn by turn, appended at run scope (run_id = cc-<session_id>)
// so memory.search / memory.ask / context.pack calls that carry the run_id
// read it back. One working_memory.append per turn is the cost — hence opt-in.

export const SCRATCHPAD_BLOCK = "scratchpad";
export const SCRATCHPAD_LINE_MAX = 240;
export const SCRATCHPAD_CHAR_LIMIT = 4000;

/** The turn's one-line conclusion: the last assistant text collapsed to one
 *  line, cut at the first sentence end past 40 chars or at the cap. */
export function turnConclusion(lastText, max = SCRATCHPAD_LINE_MAX) {
  const t = String(lastText || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!t) return "";
  const m = t.slice(40).match(/[.!?](\s|$)/);
  const cut = m ? 40 + m.index + 1 : t.length;
  const line = t.slice(0, Math.min(cut, max)).trim();
  return line.length < t.length && line.length >= max ? line.slice(0, max - 1) + "…" : line;
}

/** Body for working_memory.append at run scope: agent identity, run axis
 *  set, session/user left at "-" like the session note, so a context.pack or
 *  memory.ask that names only the run_id finds both. */
export function scratchpadAppendBody(scope, runId, line, writtenAt) {
  const when = (writtenAt || new Date().toISOString()).slice(11, 16);
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    run_id: String(runId || "-"),
    block_key: SCRATCHPAD_BLOCK,
    addition: `- ${when}Z ${redactHead(line, SCRATCHPAD_LINE_MAX).slice(0, SCRATCHPAD_LINE_MAX)}`,
    separator: "\n",
  };
}
