import { execFileSync } from "node:child_process";
import { recalledDataBlock } from "./prompt-recall.mjs";

// Decision policy and deny text for the loom-code-shunt PreToolUse hook.
// Pure functions of what the hook hands in (runLocalGrep, the one exception,
// takes its exec as a parameter), so every rule is unit-testable.
//
// An agent spends most of its tokens on reflexive I/O, whole-file reads and
// tree-wide greps, before it thinks. The index already holds every symbol
// with its path, line span, callers and as-of commit, so the shunt redirects
// the BULK reflex to it; anything targeted passes.
//
// A refusal costs the agent a request, so it must hand over what the call was
// after: a read is refused only with the declarations of the lines it asked
// for, a symbol grep only with the one definition the index holds. When the
// index did not answer in full, the call runs.
//
// The index describes merged code at its as-of commit. A file the checkout
// modified or git does not track always passes: only the working tree can
// answer for it (blast radius covers the diff after an edit).
//
// Shell parsing lives in shell-command.mjs, accounting in shunt-ledger.mjs,
// which repository the checkout is in repo-scope.mjs.

export const DEFAULT_MAX_LINES = 800;

// Read-window cap. A RANGED read (offset/limit given) of an indexed,
// unmodified file whose span is longer than this many lines is redirected to
// the enclosing unit; 0 = off, the default until a bench sets one. The cap
// has a per-session, per-file budget so a model that insists is never denied
// the same file forever.
export const DEFAULT_READ_CAP = 0;
export const READ_CAP_DENIALS_PER_FILE = 2;

// The token budget of the code.outline call behind a read refusal: the
// refusal carries every declaration of the lines asked for, so a file whose
// outline does not fit is read, not refused.
export const DEFAULT_OUTLINE_BUDGET = 1500;

const BINARY_EXT = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".pdf",
  ".ico",
  ".zip",
  ".gz",
  ".tar",
  ".parquet",
  ".bin",
  ".so",
  ".dylib",
  ".a",
  ".o",
  ".wasm",
  ".woff",
  ".woff2",
  ".ttf",
]);

/** owner/name from any git remote URL shape, lower-cased; "" if unparseable. */
export function repoSlug(remoteUrl) {
  const u = String(remoteUrl || "")
    .trim()
    .replace(/\.git$/, "");
  const m = u.match(/[:/]([^/:]+)\/([^/]+)$/);
  return m ? `${m[1]}/${m[2]}`.toLowerCase() : "";
}

/** Is `rel` inside (or an ancestor of) one of the indexed path prefixes? */
export function underPrefixes(rel, prefixes) {
  if (!prefixes || prefixes.length === 0) return true;
  if (rel === "" || rel === ".") return true; // repo root scope covers them all
  const p = rel.endsWith("/") ? rel : rel + "/";
  return prefixes.some((pre) => p.startsWith(pre) || pre.startsWith(p));
}

export function looksBinary(rel) {
  const dot = rel.lastIndexOf(".");
  return dot >= 0 && BINARY_EXT.has(rel.slice(dot).toLowerCase());
}

// ---------------------------------------------------------------------------
// Pass reasons. Every pass names WHY the hook let the call
// through, from this closed vocabulary — it becomes `source` on the
// shunt_pass row the server keeps, so `GROUP BY source` answers "what share
// of pass tokens is the modified-file rule?".
// LowCardinality-friendly: never a path, never free text. The server keeps
// the same list (server/code/api/models_outcome.py SHUNT_PASS_REASONS).
// ---------------------------------------------------------------------------

export const PASS_REASONS = Object.freeze([
  "modified", // tracked file differs from HEAD: the index cannot describe it
  "untracked", // file git does not know: never indexed, cannot be shunted
  "targeted_span", // Read with offset/limit: the model already knows where to look
  "under_max_lines", // small file: cheaper to read than to redirect
  "not_indexed", // path outside the indexed prefixes
  "binary", // no symbols to offer
  "single_file_grep", // grep inside one file is targeted
  "file_grep_lines", // one-file grep asking for line numbers or context, run as content
  "tree_content_grep", // tree-wide grep for matching lines: cheaper than its redirect
  "file_list_grep", // tree-wide grep that lists files or counts matches: no matching lines to flood
  "cap_spent", // ranged read over the read-window cap, but this file's denial budget is spent
  "symbol_grep_spent", // single-file grep for a symbol, but this symbol's denial budget is spent
  "budget_unrecorded", // a denial whose budget spend could not be written: passed rather than lock the agent out
  "compound_command", // a Bash command of several parts: a refusal would withhold every part
  "index_unanswered", // the index did not answer the lookup in full: nothing to refuse with
]);

// A grep whose pattern names one symbol is a symbol lookup: the index is
// asked once per symbol per session, and the grep is denied with its answer.
export const SYMBOL_GREP_DENIALS_PER_SYMBOL = 1;

/** The symbol a grep pattern names, or "": a `Class::method` path, or a
 *  mixed-case / snake_case identifier of eight or more characters with a
 *  lowercase letter (an ALL_CAPS constant or a short word is a string, not a
 *  symbol). Anchors, `\\b`, quotes and a trailing `(` are stripped first. */
export function symbolInPattern(pattern) {
  let q = String(pattern || "").trim();
  q = q
    .replace(/^["']|["']$/g, "")
    .replace(/^\^/, "")
    .replace(/\$$/, "");
  q = q
    .replace(/^\\b/, "")
    .replace(/\\b$/, "")
    .replace(/\\?\($/, "")
    .trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*(?:::~?[A-Za-z_][A-Za-z0-9_]*)*$/.test(q)) return "";
  if (q.includes("::")) return q;
  if (q.length >= 8 && /[a-z]/.test(q) && (/[a-z][A-Z]/.test(q) || q.includes("_"))) return q;
  return "";
}

/** Has this session already denied `rel` READ_CAP_DENIALS_PER_FILE times?
 *  `counts` is the per-session map {rel: denials} the hook keeps on disk;
 *  anything unreadable counts as zero (fail-open). */
export function capBudgetSpent(counts, rel, perFile = READ_CAP_DENIALS_PER_FILE) {
  const n = Number(counts && typeof counts === "object" ? counts[rel] : 0);
  return Number.isFinite(n) && n >= perFile;
}

/** The counts map after one more denial of `rel` (a new object). */
export function spendCapBudget(counts, rel) {
  const base = counts && typeof counts === "object" ? counts : {};
  const n = Number(base[rel]);
  return { ...base, [rel]: (Number.isFinite(n) && n > 0 ? n : 0) + 1 };
}

/** The ranged-read verdict under the read-window cap, or null when the cap
 *  does not apply (off, whole-file read, span within the cap, unknown length).
 *  Rule order: a modified or untracked file is never capped — the index
 *  cannot describe it, so the read is the only way to the truth. */
function classifyRangedRead(ctx) {
  const cap = Number(ctx.readCap ?? DEFAULT_READ_CAP);
  if (!(cap > 0)) return null;
  const { rel, prefixes, modified } = ctx;
  if (looksBinary(rel) || !underPrefixes(rel, prefixes) || modified) return null;
  const span = readSpan(ctx);
  if (!span.start || span.end - span.start + 1 <= cap) return null;
  if (capBudgetSpent(ctx.capCounts, rel)) return { deny: null, reason: "cap_spent" };
  return { deny: denyCapText(ctx, span, cap), reason: "hit", capped: true };
}

/**
 * Classify a Read call. `ctx` = {rel, prefixes, lineCount, modified, offset, limit,
 * readCap, capCounts, declarations}. `declarations` are the index's, for the
 * lines asked for (declarationsInSpan); the denial text lists them. The hook
 * asks for them once the verdict is a hit and refuses only when it holds them.
 * `modified` may be a boolean or the git
 * status class ("modified" | "untracked"). Returns {deny, reason}: deny is the
 * denial text (null on a pass), reason is "hit" for a deny and one of
 * PASS_REASONS for a pass; a hit under the read-window cap also carries
 * `capped: true`. Rule ORDER is the attribution: a targeted read of a
 * modified file is targeted_span, a whole-file read of one is `modified`;
 * a ranged read over `readCap` lines of an indexed, unmodified file is a hit
 * until the file's denial budget is spent, then `cap_spent`.
 */
export function classifyRead(ctx, maxLines = DEFAULT_MAX_LINES) {
  const { rel, prefixes, lineCount, modified } = ctx;
  const pass = (reason) => ({ deny: null, reason });
  if (rel == null || rel === "") return pass("not_indexed");
  if (ctx.offset != null || ctx.limit != null) {
    // A ranged read has a window too. `capped: true` on the
    // verdict tells the hook to spend one of the file's denials.
    return classifyRangedRead(ctx) || pass("targeted_span");
  }
  if (looksBinary(rel)) return pass("binary");
  if (!underPrefixes(rel, prefixes)) return pass("not_indexed"); // nothing better to offer
  if (modified) return pass(modified === "untracked" ? "untracked" : "modified");
  if (!(lineCount > maxLines)) return pass("under_max_lines");
  return { deny: denyReadText(ctx), reason: "hit" };
}

/** The redirect names the repository's own namespace so the
 *  substituted call lands on THIS index, not the deployment default. */
function scopeSentence(ctx) {
  if (!ctx || !ctx.namespace) return "";
  return ` Pass namespace: "${ctx.namespace}" on that call — this checkout's index.`;
}

/** The outline entries a read of `span` would show: the declarations that
 *  overlap it, in source order. [] when `outline` (a code.outline answer) does
 *  not hold them all: nothing indexed for the file, or a list its budget cut
 *  at or before the span's last line. */
export function declarationsInSpan(outline, span) {
  const entries = (Array.isArray(outline?.entries) ? outline.entries : []).filter(
    (e) => e && e.qualified_name && e.start_line > 0,
  );
  if (!entries.length || !span?.start) return [];
  if (Number(outline.elided) > 0 && entries[entries.length - 1].start_line <= span.end) return [];
  return entries.filter((e) => e.start_line <= span.end && Math.max(e.end_line || 0, e.start_line) >= span.start);
}

/** One line per declaration: its lines, its qualified name, its header. */
function declarationLines(declarations) {
  return (Array.isArray(declarations) ? declarations : [])
    .map((e) => {
      const header = String(e.signature || "")
        .replace(/\s+/g, " ")
        .trim();
      return `${e.start_line}-${e.end_line || e.start_line} ${e.qualified_name}${header ? ` ${header}` : ""}`;
    })
    .join("\n");
}

const READ_DIRECTLY = "Uncommitted edits are invisible to the index: if you changed this file, read it directly.";

function denyCapText(ctx, span, cap) {
  const { rel } = ctx;
  const n = span.end - span.start + 1;
  return (
    `loom-code-shunt: Read of ${rel} lines ${span.start}-${span.end} is ${n} lines, over the ` +
    `${cap}-line read window, and the file is in loom's code index` +
    `${ctx.asOf ? ` (as of ${ctx.asOf.slice(0, 12)})` : ""}. The declarations in those lines ` +
    `(lines, qualified name, header):\n${declarationLines(ctx.declarations)}\n` +
    `Read the ones you need, at most ${cap} lines at a time, or call loom_code_symbol ` +
    `name=<qualified name> for one definition with its callers. ${READ_DIRECTLY}` +
    scopeSentence(ctx)
  );
}

function denyReadText(ctx) {
  const { rel, lineCount } = ctx;
  return (
    `loom-code-shunt: ${rel} is ${lineCount} lines and is in loom's code index` +
    `${ctx.asOf ? ` (as of ${ctx.asOf.slice(0, 12)})` : ""}. Its declarations ` +
    `(lines, qualified name, header):\n${declarationLines(ctx.declarations)}\n` +
    `Read the lines you need (a ranged read passes this hook), or call loom_code_symbol ` +
    `name=<qualified name> for one definition with its callers. ${READ_DIRECTLY}` +
    scopeSentence(ctx)
  );
}

/**
 * Classify a Grep call. `ctx` = {rel (of the search root), isFile, prefixes, pattern,
 * symbol, symbolBudgetSpent, answer}. Returns {deny, reason} like classifyRead; a
 * root outside the repo is `not_indexed` (the hook does not log it — nothing
 * indexed was in play). A symbol hit names `symbol`; its text carries `answer`,
 * the index's definition (wholeSymbolAnswer), which the hook asks for and
 * without which it lets the grep run.
 */
/** A Grep call with no output_mode that passes a line-only flag (`-n`,
 *  `-A`, `-B`, `-C`, `context`): the caller wants matching lines. */
export function wantsLines(input) {
  if (!input || input.output_mode) return false;
  return ["-n", "-A", "-B", "-C", "context"].some((k) => input[k] !== undefined && input[k] !== false);
}

export function classifyGrep(ctx) {
  const { rel, isFile, prefixes } = ctx;
  const pass = (reason) => ({ deny: null, reason });
  if (rel == null) return pass("not_indexed"); // outside the repo
  if (isFile) {
    if (ctx.modified) return pass(ctx.modified === "untracked" ? "untracked" : "modified");
    // A single-file grep for one symbol is a symbol lookup.
    if (ctx.symbol && underPrefixes(rel, prefixes)) {
      if (ctx.symbolBudgetSpent) return pass("symbol_grep_spent");
      return { deny: denySymbolGrepWithAnswer(ctx, ctx.answer || ""), reason: "hit", symbol: ctx.symbol };
    }
    return pass("single_file_grep");
  }
  if (!underPrefixes(rel, prefixes)) return pass("not_indexed");
  // A list of files or of counts is the cheap way to locate code.
  if (ctx.outputMode && ctx.outputMode !== "content") return pass("file_list_grep");
  // Matching lines pass too: measured in paired agent runs, a tree-wide content grep
  // returned a median ~500 tokens, and its redirect cost a turn plus a code.grep
  // answer about twice that size.
  return pass("tree_content_grep");
}

/** The local-first switch, OFF by default and
 *  the shunt's only opt-out short of LOOM_CODE_SHUNT=off.
 *  `LOOM_CODE_LOCAL_FIRST=on` (or the plugin's `local_first` option, which
 *  Claude Code exports as CLAUDE_PLUGIN_OPTION_LOCAL_FIRST) says "I work from
 *  my local tree": the shunt stands down entirely, whatever the commits. */
export function localFirstOn(env = process.env) {
  const v = String(env.LOOM_CODE_LOCAL_FIRST || env.CLAUDE_PLUGIN_OPTION_LOCAL_FIRST || "")
    .trim()
    .toLowerCase();
  return v === "on" || v === "true" || v === "1" || v === "yes";
}

/** The index's answer to "where is this symbol defined".
 *  `result` is a code.symbol response; `asOf` the indexed commit the
 *  session-start cache recorded (information, never a condition). "" when
 *  the index has no definition.
 *  `localGrep` / `dirtyFiles`: the caller's own lines from named files it has
 *  modified, the one thing the index cannot know. */
export function symbolAnswerText(result, { asOf = "", namespace = "", localGrep = "", dirtyFiles = [] } = {}) {
  const defs = Array.isArray(result?.definitions) ? result.definitions : [];
  const d = defs.find((x) => x && x.path && x.start_line);
  if (!d) return "";
  const at = String(asOf || "").slice(0, 9);
  const span = d.end_line && d.end_line !== d.start_line ? `${d.start_line}-${d.end_line}` : `${d.start_line}`;
  const where = [`${namespace || "index"}${at ? ` @ ${at}` : ""}`, d.tier ? `${d.tier} tier` : ""]
    .filter(Boolean)
    .join(", ");
  let text =
    `Index answer: ${d.symbol || "the symbol"} is defined at ${d.path}:${span} (${where})` +
    `${defs.length > 1 ? `; ${defs.length - 1} more definition(s) — call loom_code_symbol for all` : ""}.`;
  if (localGrep) {
    const changed = dirtyFiles.length ? dirtyFiles.join(", ") : "the files you named";
    text += ` You have local changes in ${changed}; your lines from there follow, so no grep is needed:\n${localGrep}`;
  }
  return text;
}

/** The symbol answer a refusal may carry, or "" when it is not whole: the
 *  index holds exactly one definition (several are a choice the caller's own
 *  grep settles), and the caller's lines from modified files all fit
 *  (`localGrep` is null when they do not). */
export function wholeSymbolAnswer(result, opts = {}) {
  const defs = Array.isArray(result?.definitions) ? result.definitions : [];
  return defs.length === 1 && opts.localGrep !== null ? symbolAnswerText(result, opts) : "";
}

/** The grep the model asked for, run by the hook over the named files the
 *  caller has modified. `files` are the paths the model named; `grep` runs
 *  directly, no shell, with the loom grep shim switched off so a shim on
 *  PATH cannot call back into Loom. Fixed-string match on the pattern; ""
 *  on no match, error or timeout. */
export function runLocalGrep(cwd, pattern, files, { exec = execFileSync, timeoutMs = 2000, env = process.env } = {}) {
  const names = (Array.isArray(files) ? files : []).map((f) => String(f || "")).filter(Boolean);
  if (!pattern || !names.length) return "";
  try {
    return String(
      exec("grep", ["-n", "-F", "--", String(pattern), ...names], {
        cwd,
        env: { ...env, LOOM_GREP_SHIM: "off" },
        encoding: "utf8",
        timeout: timeoutMs,
        maxBuffer: 256 * 1024,
        stdio: ["ignore", "pipe", "ignore"],
        windowsHide: true,
      }) || "",
    );
  } catch {
    return ""; // exit 1 = no match; anything else reads as no lines
  }
}

/** The local grep's lines with the command and HEAD named; "" for no lines,
 *  null when they exceed `maxLines` or `maxChars`: a cut list is not the
 *  grep's answer, so the caller lets the grep run. */
export function localGrepText(output, { pattern = "", files = [], head = "", maxLines = 12, maxChars = 1500 } = {}) {
  const lines = String(output || "")
    .split("\n")
    .filter(Boolean);
  if (!lines.length) return "";
  const body = lines.join("\n");
  if (lines.length > maxLines || body.length > maxChars) return null;
  const cmd = `grep -n ${JSON.stringify(String(pattern))} ${files.join(" ")}`.trim();
  return `${cmd}${head ? ` (HEAD ${String(head).slice(0, 9)})` : ""}:\n${body}`;
}

/** The symbol-grep denial: the index's answer first, the callers pointer,
 *  the scope — no round trip left for the model to make. */
export function denySymbolGrepWithAnswer(ctx, answer) {
  const { pattern, symbol } = ctx;
  return (
    `loom-code-shunt: ${JSON.stringify(String(pattern))} names a symbol. ${answer}${answer.includes("\n") ? "\n" : " "}` +
    `For its callers call loom_code_symbol name=${JSON.stringify(String(symbol))} uses_brief: true.` +
    scopeSentence(ctx)
  );
}

// ---------------------------------------------------------------------------
// A GitHub issue search from an indexed checkout is answered
// by the tracker record first: asked "is this already reported?", a model
// otherwise spends several `gh issue list --search` calls (often empty —
// GitHub's search wants the exact phrase) while tracker.duplicate_check has
// the calibrated answer in about a second. Pure helpers: the shape of the
// command, the answer text, the denial text.
// ---------------------------------------------------------------------------

/** How many times one search query is denied per session before it passes:
 *  the model saw the QA answer once; a repeat is a deliberate GitHub search. */
export const TRACKER_SEARCH_DENIALS_PER_QUERY = 1;

/** The repository a gh search asks about when it is not the checkout's own:
 *  its slug, else "" (no `--repo`, or the checkout's repository). */
export function otherSearchRepo(search, checkoutRepo) {
  const named = String((search && search.repo) || "").toLowerCase();
  return named && named !== String(checkoutRepo || "").toLowerCase() ? named : "";
}

/** One line per tracker item: `#123 title (state, 0.71)`; items are the
 *  duplicate-check output's `items[]` (number, title, state, score, url) or
 *  its `nearest[]` (the same, with `confidence` in place of `score`).
 *  Up to `max` lines. */
function trackerItemLines(items, max = 5) {
  const out = [];
  for (const it of (Array.isArray(items) ? items : []).slice(0, max)) {
    if (!it || typeof it !== "object") continue;
    const n = it.number;
    const title = String(it.title || "").trim();
    if (!n && !title) continue;
    const state = it.state ? String(it.state) : "";
    const score = Number(it.score ?? it.confidence);
    const tail = [state, Number.isFinite(score) ? score.toFixed(2) : ""].filter(Boolean).join(", ");
    // A check spans every tracker record the caller may read, so
    // an item names its record — #35677 of the public repo is not #35677 of
    // the private one.
    const ns = it.namespace ? `[${it.namespace}] ` : "";
    out.push(`- ${ns}#${n ?? "?"} ${title}${tail ? ` (${tail})` : ""}${it.url ? ` ${it.url}` : ""}`);
  }
  return out;
}

/** Item lines as recalled data: anyone who can file an issue writes its title. */
function fencedItems(lines) {
  return lines.length ? [recalledDataBlock(lines.join("\n"))] : [];
}

/** The tracker record's answer to an issue search, or "" when the record could
 *  not answer (tracker_unavailable, an error shape, no verdict): then gh runs.
 *  The heads are the plugin's; the item lines go inside recalled-data blocks. */
export function trackerAnswerText(result, { query = "", namespace = "" } = {}) {
  if (!result || typeof result !== "object" || result.tracker_unavailable || !result.verdict) return "";
  const verdict = String(result.verdict);
  const items = Array.isArray(result.items) ? result.items : [];
  const lines = trackerItemLines(items);
  const read = Array.isArray(result.namespaces) && result.namespaces.length > 1 ? result.namespaces.join(" + ") : "";
  const where = read ? ` (${read})` : namespace ? ` of ${namespace}` : "";
  const q = JSON.stringify(String(query));
  let head;
  // The nearest on-topic issues under the related line —
  // for the reader to check by eye, never presented as matches.
  const nearest = trackerItemLines(Array.isArray(result.nearest) ? result.nearest : []);
  const nearestHead = "nearest reported issues, below the related line (NOT duplicates — check by eye):";
  if (verdict === "duplicate_of" || verdict === "duplicate") {
    head = `tracker record${where}: ${q} IS already reported — ${lines.length ? `the same defect (${lines.length}):` : "a duplicate exists."}`;
  } else if (lines.length) {
    head = `tracker record${where}: ${q} has no likely duplicate (verdict ${verdict}); the closest reported items (${lines.length}):`;
  } else if (nearest.length) {
    return [
      `tracker record${where}: ${q} has no likely duplicate (verdict ${verdict}, ${result.epistemic || "calibrated"}); ${nearestHead}`,
      ...fencedItems(nearest),
    ].join("\n");
  } else {
    head = `tracker record${where}: ${q} has no likely duplicate in the tracker index (verdict ${verdict}, ${result.epistemic || "calibrated"}${result.undecided ? ", undecided" : ""}).`;
  }
  return [head, ...fencedItems(lines), ...(nearest.length ? [nearestHead, ...fencedItems(nearest)] : [])].join("\n");
}

/** The gh-search denial when the tracker record answered: the answer, then the
 *  calibrated follow-up, then the way to GitHub for a deliberate search. */
export function denyGhSearchWithAnswer(ctx, answer) {
  const scope = ctx && ctx.namespace ? ` (namespace: ${JSON.stringify(String(ctx.namespace))})` : "";
  return (
    `loom-code-shunt: this GitHub search is answered by Loom's tracker record — the issues and PRs of this ` +
    `repository, searched by meaning, stack frames and words, not by exact phrase.\n${answer}\n` +
    `For the calibrated verdict on a draft report call loom_tracker_duplicate_check with title, body and frames` +
    `${scope}; loom_tracker_similar ranks more candidates. Re-run this exact gh command to search GitHub anyway ` +
    `(it passes on the next try), or set LOOM_TRACKER_SHUNT=off.`
  );
}

/** Claude Code's Read returns this many lines when `limit` is omitted. */
export const DEFAULT_READ_LIMIT = 2000;

/** The 1-based line span a Read covers: whole file when offset/limit are
 *  absent, else offset..offset+limit-1 clipped to the file (limit defaults to
 *  Claude Code's 2000). {start:0,end:0} when the file's length is unknown. */
export function readSpan({ lineCount, offset, limit }) {
  const n = Number(lineCount);
  if (!Number.isFinite(n) || n <= 0) return { start: 0, end: 0 };
  const start = Math.max(1, Math.floor(Number(offset) || 1));
  const want = limit != null && Number(limit) > 0 ? Math.floor(Number(limit)) : DEFAULT_READ_LIMIT;
  const end = Math.min(n, start + want - 1);
  return end >= start ? { start, end } : { start: 0, end: 0 };
}
