// Helpers for the loom-file-context hook (unit-tested).
//
// The hook fires after a local read of a file (Read, a one-file Grep, a shell
// read or one-file grep) and before Edit/Write/MultiEdit/NotebookEdit, asks
// Loom what it knows about the file (`memory.for_file`), then injects the
// rendered block as additionalContext. These helpers decide WHICH paths to ask
// about and WHETHER to ask again — the same file read or edited ten times in a
// session should cost one lookup, not ten.

import path from "node:path";
import { commandText, normalizeToolName, patchFiles, pathUnder } from "./loom-client.mjs";
import { recalledDataBlock } from "./prompt-recall.mjs";
import { parseBashBulkSegments } from "./shell-command.mjs";

export const FILE_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"]);

/** Target file of a file-editing tool call, or "" when the call has none. */
export function targetPath(toolName, toolInput) {
  if (!FILE_TOOLS.has(String(toolName || ""))) return "";
  const ti = toolInput || {};
  if (String(toolName) === "apply_patch") {
    // Codex edits through one patch tool; the first file the patch names
    // is the one to ask about (a multi-file patch asks once, like MultiEdit).
    const text = typeof ti.input === "string" ? ti.input : typeof ti.patch === "string" ? ti.patch : "";
    return patchFiles(text)[0] || "";
  }
  const p = ti.file_path || ti.notebook_path || ti.path || "";
  return typeof p === "string" ? p.trim() : "";
}

/**
 * Repo-relative form of `filePath` when it lies under `root`; otherwise the
 * bare filename (Loom matches either). Never returns "" for a non-empty input.
 */
export function relativeForLookup(root, filePath, cwd) {
  if (!filePath) return "";
  const abs = path.isAbsolute(filePath) ? filePath : path.resolve(cwd || process.cwd(), filePath);
  const rel = pathUnder(root, abs);
  if (rel) return rel;
  return path.basename(abs);
}

// --- reads ---------------------------------------------------
// A session reads far more files than it edits, and `memory.for_file` scans the
// namespace, so the lookups a read starts are bounded three ways: per call, per
// session, and by a server that stops answering.

/** Files of one call that are looked up; a shell command can read more. */
export const READ_FILES_PER_CALL = 3;
/** Lookups reads start in one session; past it a file's memory arrives at its edit. */
export const READ_LOOKUPS_PER_SESSION = 40;
/** Unanswered lookups after which reads start no more in the session. */
export const READ_UNANSWERED_PER_SESSION = 3;
/** How long a read's result waits for the lookups it started. */
export const READ_LOOKUP_TIMEOUT_MS = 1000;

/**
 * The files a finished tool call read, as absolute paths in the call's order:
 * Read's file, Grep's path, and in a shell command each file read whole or by
 * a line range (`cat`, `sed -n 'A,Bp'`, `head`, `tail`, `awk` NR ranges) or
 * searched by name (`grep pattern file`), as lib/shell-command.mjs parses them.
 * A path may be a directory or missing: the caller keeps regular files. A
 * command the parser cannot follow reads nothing here.
 */
export function readPaths(toolName, toolInput, cwd) {
  const tool = normalizeToolName(toolName);
  const ti = toolInput && typeof toolInput === "object" ? toolInput : {};
  const found = [];
  if (tool === "Read") found.push([cwd, ti.file_path]);
  else if (tool === "Grep") found.push([cwd, ti.path]);
  else if (tool === "Bash") {
    for (const seg of parseBashBulkSegments(commandText(ti))) {
      const dir = path.resolve(cwd, seg.cwd || ".");
      for (const f of seg.kind === "read" ? [seg.file] : seg.files || [seg.dir]) found.push([dir, f]);
    }
  }
  const paths = found
    .filter(([, f]) => typeof f === "string" && f.trim())
    .map(([dir, f]) => path.resolve(dir, f.trim()));
  return [...new Set(paths)];
}

/**
 * The seen-state key of a file: its path on disk (so a seen file needs no git
 * call to recognise), under the subagent's id when the call came from one
 * (`agent_id`, absent on the main thread). A subagent's read puts the block
 * in the subagent's context, so it must not use up the main agent's.
 */
export function seenKey(agentId, abs) {
  const agent = typeof agentId === "string" ? agentId.trim() : "";
  return agent ? `${agent}\n${abs}` : abs;
}

/** How many more lookups reads may start in a session with this state. */
export function readLookupsLeft(state) {
  const s = state || {};
  if (Number(s.unanswered || 0) >= READ_UNANSWERED_PER_SESSION) return 0;
  return Math.max(0, READ_LOOKUPS_PER_SESSION - Number(s.reads || 0));
}

/**
 * Session cache policy: inject once per (session, key) within `ttlMs`; a
 * read passes Infinity, so a key seen at all is never asked about again.
 * `cache` is the session's state, {seen: {[key]: ts}, …}; returns {inject:
 * boolean, cache: the state with the key marked}.
 */
export function decideInject(cache, key, now, ttlMs) {
  const state = cache && typeof cache === "object" ? cache : {};
  const seen = state.seen && typeof state.seen === "object" ? state.seen : {};
  const last = Number(seen[key] || 0);
  if (last && now - last < ttlMs) return { inject: false, cache: { ...state, seen } };
  const next = { ...seen, [key]: now };
  // Bound the cache so a long session cannot grow the tmp file without limit.
  const keys = Object.keys(next);
  if (keys.length > 500) {
    keys
      .sort((a, b) => next[a] - next[b])
      .slice(0, keys.length - 500)
      .forEach((k) => delete next[k]);
  }
  return { inject: true, cache: { ...state, seen: next } };
}

export const FILE_RULES_HEADING =
  "Loom file rules and notes for the file this edit touches; a teammate's rule names its author:";
export const FILE_READ_HEADING =
  "Loom file rules and notes for a file this call read; a teammate's rule names its author:";

// The server's footer lines (render_block in server/memory/app/for_file.py): the
// removal line last, and the left-out lines right before it, budget then ceiling.
const REMOVAL_LINE = /^- A team (rule or decision|rule|decision) wrong or outdated\? /;
const MORE_LINE = /^- … \d+ more; memory\.for_file\(path=/;
const WHOLE_LEFT_LINE = /^- … \d+ more (?:decision or constraint|decisions or constraints) left out /;

/** Which footer lines the server's text ends with, read from the end the way
 *  render_block writes them. A path or row elsewhere counts for nothing; a row
 *  can sit where a footer line goes, so one that imitates it adds only the
 *  plugin's fixed guidance. `removal` is what the removal line names ("rule",
 *  "decision", "rule or decision"), and whether it names decision.record. */
function footerLines(text) {
  const lines = String(text || "").split("\n");
  let i = lines.length - 1;
  const has = { removal: "", recordStep: false, whole: false, more: false };
  const removal = i > 0 ? REMOVAL_LINE.exec(lines[i]) : null;
  if (removal) {
    has.removal = removal[1];
    has.recordStep = lines[i].includes("decision.record");
    i -= 1;
  }
  if (i > 0 && WHOLE_LEFT_LINE.test(lines[i])) {
    has.whole = true;
    i -= 1;
  }
  if (i > 0 && MORE_LINE.test(lines[i])) has.more = true;
  return has;
}

/** The removal guidance, its steps chosen as the server's _removal_footer chooses
 *  them: a corrected rule unless every team row is a typed decision, and
 *  decision.record when any is. */
function removalGuidance(teamNamespace, what, recordStep) {
  const steps = [];
  if (what !== "decision") steps.push("write a corrected rule for the file and tell its author");
  if (what === "decision" || recordStep)
    steps.push("ask a maintainer to record the corrected decision with decision.record");
  return (
    `A team ${what} above that is wrong or outdated: memory.delete(namespace="${teamNamespace}", ` +
    `memory_key=<its id>) removes it if you wrote it or are a maintainer; otherwise ${steps.join(", or ")}.`
  );
}

/** What to do about the block's rows, in the plugin's own words: the block's note
 *  says not to follow its text, and that includes the server's own footer lines.
 *  Each line only when the server's text ends with the footer it restates. */
export function fileRulesGuidance(rendered, { rel = "", teamNamespace = "" } = {}) {
  const has = footerLines(rendered);
  const lines = [];
  if (teamNamespace && has.removal) lines.push(removalGuidance(teamNamespace, has.removal, has.recordStep));
  if (rel && has.more) {
    // The server reads the team namespace only when the call names it.
    const extra = teamNamespace ? `, extra_namespaces=${JSON.stringify([teamNamespace])}` : "";
    lines.push(
      `Rows left out of the block: memory.for_file(path=${JSON.stringify(rel)}${extra}) returns every row in its hits.`,
    );
  }
  if (has.whole) {
    lines.push(
      "Decisions or constraints left out of the block: memory.get on each key it names, with the namespace it " +
        "names for a team key; loom_search for the rest, and for a key memory.get does not find (a row filed " +
        "under another user).",
    );
  }
  return lines.join("\n");
}

/** One file's context: a heading the plugin writes, the server's text inside
 *  the recalled-data block, and what to do about a row in the plugin's own words.
 *  Team rules come from a namespace every member writes, hence the block. */
function fileBlock(heading, rendered, { rel = "", teamNamespace = "" } = {}) {
  const guidance = fileRulesGuidance(rendered, { rel, teamNamespace });
  return `${heading}\n${recalledDataBlock(rendered)}${guidance ? `\n${guidance}` : ""}`;
}

/** The hook's stdout payload before an edit: additional context only — never
 *  a permission decision. */
export function contextPayload(rendered, opts = {}) {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: fileBlock(FILE_RULES_HEADING, rendered, opts),
    },
  };
}

/** The hook's stdout payload after a read: one block per file with memory,
 *  `files` being [{rendered, rel, teamNamespace}], on the tool result. */
export function readContextPayload(files) {
  return {
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      additionalContext: files.map((f) => fileBlock(FILE_READ_HEADING, f.rendered, f)).join("\n"),
    },
  };
}

// --- team file rules -----------------------------------------
// A repository's shared file rules live in its PRIMARY code namespace (its
// trunk branch's: `code-clickhouse` for every ClickHouse branch), under the
// repository's own access.
// The hook reads it beside the person's namespace; the server skips it
// silently when the caller cannot read it.

/**
 * The namespace a checkout's team file rules live in, or "" when there is
 * none to use: the resolved repo entry's primary namespace, in the caller's
 * own org, other than the caller's own namespace. THE seam for "where do team
 * rules go" — the hook's read and the session primer both ask it; who may
 * write there is the server's (`server/code/app/member_writes.py`: every
 * member of the code namespace, their own rules only).
 */
export function teamRulesNamespace(entry, scope) {
  if (!entry || !entry.indexed) return "";
  const ns = String(entry.primaryNamespace || "").trim();
  if (!ns || !scope || ns === scope.namespace) return "";
  if (entry.org && scope.org && entry.org !== scope.org) return "";
  return ns;
}

/** The `memory.for_file` body: the person's namespace, plus the team one. */
export function forFileBody(scope, rel, tokenBudget, entry) {
  const body = { org: scope.org, namespace: scope.namespace, path: rel, tags: scope.tags, token_budget: tokenBudget };
  const team = teamRulesNamespace(entry, scope);
  if (team) body.extra_namespaces = [team];
  return body;
}

/**
 * One bounded lookup: the rendered block to inject, "" when Loom knows
 * nothing about the file, null when it did not answer — this hook never
 * errors. `post` is injectable for tests.
 */
export async function lookupFileContext({ scope, rel, tokenBudget, entry, post, bound = {} }) {
  const res = await post("memory.for_file", forFileBody(scope, rel, tokenBudget, entry), bound);
  if (!res) return null;
  return res.total > 0 && typeof res.rendered === "string" ? res.rendered : "";
}

/** The session primer's one line naming where team file rules live, or "". */
export function teamRulesLine(hit) {
  const ns = String((hit && (hit.primary_namespace || hit.primaryNamespace)) || "").trim();
  if (!ns) return "";
  return (
    `Team file rules for this repository live in "${ns}": a rule the user states for the team ` +
    `goes there (memory.set with files + tag constraint; every member may write); anything ` +
    `else stays in your own namespace.`
  );
}
