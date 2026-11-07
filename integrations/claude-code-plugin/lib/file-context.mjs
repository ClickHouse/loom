// Pure helpers for the loom-file-context PreToolUse hook (unit-tested; no I/O).
//
// The hook fires before Edit/Write/MultiEdit/NotebookEdit and asks Loom what it
// knows about the target file (`memory.for_file`), then injects the rendered
// block as additionalContext. These helpers decide WHICH path to ask about and
// WHETHER to ask again — the same file edited ten times in a session should
// cost one lookup, not ten.

import path from "node:path";
import { patchFiles } from "./loom-client.mjs";
import { recalledDataBlock } from "./prompt-recall.mjs";

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
  if (root) {
    const rel = path.relative(root, abs);
    if (rel && !rel.startsWith("..") && !path.isAbsolute(rel)) return rel.split(path.sep).join("/");
  }
  return path.basename(abs);
}

/**
 * Session cache policy: inject once per (session, path) within `ttlMs`.
 * `cache` is {seen: {[key]: ts}}; returns {inject: boolean, cache: updated}.
 */
export function decideInject(cache, key, now, ttlMs) {
  const seen = (cache && cache.seen) || {};
  const last = Number(seen[key] || 0);
  if (last && now - last < ttlMs) return { inject: false, cache: { seen } };
  const next = { ...seen, [key]: now };
  // Bound the cache so a long session cannot grow the tmp file without limit.
  const keys = Object.keys(next);
  if (keys.length > 500) {
    keys
      .sort((a, b) => next[a] - next[b])
      .slice(0, keys.length - 500)
      .forEach((k) => delete next[k]);
  }
  return { inject: true, cache: { seen: next } };
}

export const FILE_RULES_HEADING =
  "Loom file rules and notes for the file this edit touches; a teammate's rule names its author:";

// The server's footer lines (server/memory/app/for_file.py) the guidance restates.
const REMOVAL_LINE = /\n- A team /;
const MORE_LINE = /\n- … \d+ more; memory\.for_file\(/;
const WHOLE_LEFT_LINE = /\n- … \d+ more (?:decision or constraint|decisions or constraints) left out /;

/** What to do about the block's rows, in the plugin's own words: the block's note
 *  says not to follow its text, and that includes the server's own footer lines.
 *  Each line only when the server's text has the footer it restates (the removal
 *  line, the budget's left-out line, the left-out decisions and constraints);
 *  stored text that imitates one adds only this fixed text. */
export function fileRulesGuidance(rendered, { rel = "", teamNamespace = "" } = {}) {
  const text = String(rendered || "");
  const lines = [];
  if (teamNamespace && REMOVAL_LINE.test(text)) {
    lines.push(
      `A team rule or decision above that is wrong or outdated: memory.delete(namespace="${teamNamespace}", ` +
        "memory_key=<its id>) removes it if you wrote it or are a maintainer. Otherwise a corrected rule for the " +
        "file supersedes a wrong rule (tell its author); a typed decision is corrected only by a maintainer's decision.record.",
    );
  }
  if (rel && MORE_LINE.test(text)) {
    lines.push(
      `Rows left out for the token budget: memory.for_file(path=${JSON.stringify(rel)}) with a larger token_budget lists them.`,
    );
  }
  if (WHOLE_LEFT_LINE.test(text)) {
    lines.push("Decisions or constraints left out of the block: memory.get on each key it names reads it.");
  }
  return lines.join("\n");
}

/** The hook's stdout payload: additional context only — never a permission decision.
 *  Team rules come from a namespace every member writes, so the text goes inside the
 *  recalled-data block under a heading the plugin writes, and what to do about a row
 *  follows it in the plugin's own words. */
export function contextPayload(rendered, { rel = "", teamNamespace = "" } = {}) {
  const guidance = fileRulesGuidance(rendered, { rel, teamNamespace });
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: `${FILE_RULES_HEADING}\n${recalledDataBlock(rendered)}${guidance ? `\n${guidance}` : ""}`,
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
 * One bounded lookup; the rendered block to inject, or "" (nothing known, or
 * no answer — this hook never errors). `post` is injectable for tests.
 */
export async function lookupFileContext({ scope, rel, tokenBudget, entry, post, bound = {} }) {
  const res = await post("memory.for_file", forFileBody(scope, rel, tokenBudget, entry), bound);
  return res && res.total > 0 && typeof res.rendered === "string" ? res.rendered : "";
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
