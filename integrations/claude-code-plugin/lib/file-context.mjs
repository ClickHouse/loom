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

/** The hook's stdout payload: additional context only — never a permission decision.
 *  Team rules come from a namespace every member writes, and memory.for_file does
 *  not escape them. These left angle brackets become `&lt;`: ASCII `<`, fullwidth
 *  U+FF1C, small-form U+FE64 and U+226E (not-less-than), so none of them can open
 *  or close a tag inside the block; `>` stays as written so text like
 *  `memory_key=<id>` still reads. The text goes inside the recalled-data block under a heading the
 *  plugin writes. */
export function contextPayload(rendered) {
  const safe = String(rendered || "").replace(/[<\uFF1C\uFE64\u226E]/g, "&lt;");
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      additionalContext: `${FILE_RULES_HEADING}\n${recalledDataBlock(safe)}`,
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
