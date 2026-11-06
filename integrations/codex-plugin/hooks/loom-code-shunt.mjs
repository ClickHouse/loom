#!/usr/bin/env node
// loom-code-shunt — PreToolUse (Read | Grep | Bash): redirect the bulk-read
// reflex (whole-file Read, tree-wide Grep, `cat`/`rg`/`sed -n` in Bash)
// inside a loom-indexed repository to the code ops, and answer a GitHub
// issue search from the tracker record. Policy: lib/code-shunt.mjs.
//
// Input: the host's PreToolUse JSON on stdin (tool_name, tool_input, cwd,
// session_id). Output: nothing (the call proceeds) or one `deny` whose
// reason names the op to call instead. Nothing else ever blocks.
//
// Fail-open: no token, no repository or an unindexed one, an unanswered
// lookup, a modified or untracked file, a targeted read → the call proceeds.
// Bounds: the repository lookup (cached 10 min, 1 min when unanswered) has
// LOOM_SHUNT_RESOLVE_TIMEOUT_MS (8 s); a symbol answer 2.5 s; a tracker
// answer LOOM_TRACKER_SHUNT_TIMEOUT_MS (3 s); the whole process exits by
// DEADLINE_MS.
//
// Side effects, all in $TMPDIR: the shared repository cache
// (lib/repo-scope.mjs); per-session denial budgets for the read-window cap,
// symbol greps and tracker searches (sessionStatePath "shunt-cap"); the
// decision log loom-shunt.log and its unflushed per-session copy
// (sessionStatePath "shunt-pending"), all through lib/tmp-state.mjs's
// private-file helpers,
// flushed as code.outcome rows (fire-and-forget, 300 ms) on a deny or every
// SHUNT_FLUSH_EVERY lines (lib/shunt-ledger.mjs).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import {
  git,
  postBounded,
  postFireAndForget,
  readStdinJson,
  redactSecrets,
  scheduleExit,
  commandText,
  normalizeToolName,
  envSwitch,
} from "../lib/loom-client.mjs";
import {
  DEFAULT_MAX_LINES,
  DEFAULT_READ_CAP,
  READ_CAP_DENIALS_PER_FILE,
  SYMBOL_GREP_DENIALS_PER_SYMBOL,
  TRACKER_SEARCH_DENIALS_PER_QUERY,
  capBudgetSpent,
  classifyGrep,
  wantsLines,
  classifyRead,
  denyGhSearchWithAnswer,
  denySymbolGrepWithAnswer,
  localFirstOn,
  localGrepText,
  otherSearchRepo,
  readSpan,
  relPath,
  runLocalGrep,
  spendCapBudget,
  symbolAnswerText,
  symbolInPattern,
  trackerAnswerText,
} from "../lib/code-shunt.mjs";
import { ghIssueSearchInCommand, parseBashBulkSegments } from "../lib/shell-command.mjs";
import {
  aggregateShuntLog,
  estimateTokensRead,
  estimateTokensSaved,
  formatShuntLine,
  shouldFlushShunt,
} from "../lib/shunt-ledger.mjs";
import { repoLookupOutcome, resolveIndexedRepo, resolveTimeoutMs } from "../lib/repo-scope.mjs";
import {
  readPrivateFile,
  readSessionState,
  removeFile,
  sessionStatePath,
  tmpStatePath,
  writePrivateFile,
  writeSessionState,
} from "../lib/tmp-state.mjs";

const ENABLED = envSwitch("LOOM_CODE_SHUNT", true);
const MAX_LINES = Number(process.env.LOOM_SHUNT_MAX_LINES || DEFAULT_MAX_LINES);
const READ_CAP = Math.max(0, Number(process.env.LOOM_SHUNT_READ_CAP || DEFAULT_READ_CAP) || 0);
// The deny text stops naming its own bypass unless asked to.
const SHUNT_GREP = envSwitch("LOOM_SHUNT_GREP", true);
const SHUNT_BASH = envSwitch("LOOM_SHUNT_BASH", true);
// `gh issue list --search` / `gh search issues` inside an
// indexed checkout is answered by the tracker record first. `LOOM_TRACKER_SHUNT=off`.
const SHUNT_TRACKER = envSwitch("LOOM_TRACKER_SHUNT", true);
const TRACKER_SEARCH_TIMEOUT_MS = Math.max(500, Number(process.env.LOOM_TRACKER_SHUNT_TIMEOUT_MS || 3000) || 3000);
const LOG_ON = envSwitch("LOOM_SHUNT_LOG", true);
const REPORT_ON = envSwitch("LOOM_SHUNT_REPORT", true);
const FLUSH_TIMEOUT_MS = 300;
// The log keeps its fixed, documented name; the batch is internal state.
const LOG_PATH = path.join(os.tmpdir(), "loom-shunt.log");
// The unflushed batch, one per session (set in main): a name nobody can guess
// ahead of the session, swept with the other state files.
let PENDING_PATH = "";

function countLines(abs) {
  try {
    const st = fs.statSync(abs);
    if (!st.isFile()) return 0;
    if (st.size > 4 * 1024 * 1024) return Number.MAX_SAFE_INTEGER; // huge: certainly bulk
    const buf = fs.readFileSync(abs);
    let n = 0;
    for (let i = 0; i < buf.length; i++) if (buf[i] === 10) n++;
    return n + (buf.length && buf[buf.length - 1] !== 10 ? 1 : 0);
  } catch {
    return 0;
  }
}

/** "" when the file matches HEAD, "untracked" when git does not know it,
 *  "modified" otherwise — either way the index cannot describe it, but the
 *  two are different addressable volumes for a working-tree overlay. */
function gitStatusClass(root, rel) {
  const status = git(["status", "--porcelain", "--", rel], root);
  if (status === "") return "";
  return status.startsWith("??") ? "untracked" : "modified";
}

/** The call's input replaced (Claude Code runs the tool with these
 *  arguments; the permission flow is the tool's own). */
function rewrite(updatedInput) {
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", updatedInput } }) + "\n");
}

function deny(reason) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: reason,
      },
    }) + "\n",
  );
}

/** Per-session denial counts, {key: n}, for three budgets: over-cap reads (the
 *  file's path), symbol greps (`sym:`) and answered tracker searches (`qa:`).
 *  {} when the file is missing or refused. A denial is given only when its
 *  spend was written: when the file cannot be kept (tmp not writable, or a file
 *  planted there) the call passes as if its budget were spent, so a planted
 *  file cannot lock the agent out. */
function readCapCounts(sessionId) {
  return readSessionState("shunt-cap", sessionId);
}

/** True when the spend was kept. */
function writeCapCounts(sessionId, counts) {
  return writeSessionState("shunt-cap", sessionId, counts);
}

function fileBytes(abs) {
  try {
    const st = fs.statSync(abs);
    return st.isFile() ? st.size : 0;
  } catch {
    return 0;
  }
}

/** Append one decision locally; returns the number of unflushed lines. Sync
 *  appends of one short line — microseconds, and never on the network. */
function logDecision({ decision, tool, tokensSaved, reason, passReason, path, startLine, endLine }) {
  if (!LOG_ON) return 0;
  const line = formatShuntLine({
    decision,
    tool,
    tokensSaved,
    reason,
    passReason,
    path,
    startLine,
    endLine,
  });
  try {
    writePrivateFile(LOG_PATH, line, { append: true });
  } catch {
    /* tmp not writable, or a file we do not own: fine */
  }
  try {
    writePrivateFile(PENDING_PATH, line, { append: true });
    const text = readPrivateFile(PENDING_PATH);
    return text.split("\n").length - 1;
  } catch {
    return 0;
  }
}

/** Ship the pending batch as code.outcome rows. Fire-and-forget with a 300 ms
 *  ceiling; a lost batch costs a few counter ticks. The batch is claimed first by
 *  renaming it to a name of this process: of two hooks flushing at once only one
 *  gets it, and the claimed file is read with the private-file checks and removed,
 *  so each batch is posted at most once. */
function flushPending(repo) {
  if (!REPORT_ON || !PENDING_PATH) return;
  const claimed = tmpStatePath("shunt-claim", `${PENDING_PATH}:${process.pid}`, { ext: ".log" });
  try {
    fs.renameSync(PENDING_PATH, claimed);
  } catch {
    return; // nothing pending, or another hook claimed it
  }
  let text = "";
  try {
    text = readPrivateFile(claimed);
  } catch {
    /* planted: not ours to post */
  }
  removeFile(claimed);
  if (!text) return;
  const agg = aggregateShuntLog(text);
  const opts = { timeoutMs: FLUSH_TIMEOUT_MS };
  const scope = { org: repo.org || "", namespace: repo.namespace || "", consumer: "claude-code-shunt" };
  if (agg.hit) postFireAndForget("code.outcome", { ...scope, ...agg.hit }, opts);
  for (const body of agg.passes) postFireAndForget("code.outcome", { ...scope, ...body }, opts);
}

/** Every decision funnels through here: deny + log + maybe flush.
 *  `verdict` is classifyRead/classifyGrep's {deny, reason}; `tokens` is the
 *  estimate kept out (hit) or let in (pass); `span` the lines covered. */
function settle(repo, { tool, verdict, tokens, path = "", span = { start: 0, end: 0 } }) {
  const denied = Boolean(verdict.deny);
  if (denied) deny(verdict.deny);
  const pending = logDecision({
    decision: denied ? "hit" : "pass",
    tool,
    tokensSaved: tokens,
    reason: verdict.deny || "",
    passReason: denied ? "" : verdict.reason,
    path,
    startLine: span.start,
    endLine: span.end,
  });
  if (shouldFlushShunt({ denied, pending })) flushPending(repo);
  return denied;
}

/** Read-shaped decision (Read tool or `cat`): classify, price, span. A
 *  ranged read consults the session's cap budget first; a cap denial spends
 *  one of the file's denials. */
function settleRead(repo, { tool, rel, abs, offset, limit, sessionId, ctxBase, denyRewrite }) {
  const lineCount = countLines(abs);
  const bytes = fileBytes(abs);
  const ranged = offset != null || limit != null;
  const capCounts = READ_CAP > 0 && ranged ? readCapCounts(sessionId) : {};
  const readCtx = {
    ...ctxBase,
    rel,
    offset,
    limit,
    lineCount,
    modified: gitStatusClass(repo.root, rel),
    readCap: READ_CAP,
    capCounts,
  };
  let verdict = classifyRead(readCtx, MAX_LINES);
  if (verdict.capped && !writeCapCounts(sessionId, spendCapBudget(capCounts, rel))) {
    verdict = classifyRead({ ...readCtx, capCounts: { [rel]: READ_CAP_DENIALS_PER_FILE } }, MAX_LINES);
  }
  if (verdict.deny && denyRewrite) verdict.deny = denyRewrite(verdict.deny);
  // A whole-file hit kept the whole file out; a cap hit kept the SPAN out
  // (what the ranged read would have let in); a pass let its span in.
  const tokens =
    verdict.deny && !verdict.capped
      ? estimateTokensSaved(bytes)
      : estimateTokensRead({ bytes, lineCount, offset, limit });
  return settle(repo, { tool, verdict, tokens, path: rel, span: readSpan({ lineCount, offset, limit }) });
}

/** classifyGrep with the symbol rule's once-per-symbol budget:
 *  the budget shares the read-cap counts file under a `sym:` key. */
function classifySymbolGrep(ctx, sessionId) {
  const symbol = ctx.isFile ? symbolInPattern(ctx.pattern) : "";
  if (!symbol) return classifyGrep(ctx);
  const key = `sym:${symbol}`;
  const counts = readCapCounts(sessionId);
  const verdict = classifyGrep({
    ...ctx,
    symbol,
    symbolBudgetSpent: capBudgetSpent(counts, key, SYMBOL_GREP_DENIALS_PER_SYMBOL),
  });
  if (verdict.symbol && !writeCapCounts(sessionId, spendCapBudget(counts, key))) {
    return classifyGrep({ ...ctx, symbol, symbolBudgetSpent: true });
  }
  return verdict;
}

/** A symbol-grep denial fetches the definition itself (one bounded code.symbol
 *  call, 2.5 s) — otherwise the turn costs three model rounds (denied grep,
 *  loom_code_symbol, confirming grep) and tens of seconds when the index and
 *  the checkout disagree. The index answer is the answer, whatever commit the
 *  checkout is on; only named files the caller modified get their local grep
 *  inlined. */
async function answerSymbolDeny(verdict, repo, ctx, { cwd = repo.root, files = [] } = {}) {
  if (!verdict.deny || !verdict.symbol || !repo.namespace) return verdict;
  try {
    const res = await postBounded(
      "code.symbol",
      {
        org: repo.org || "",
        namespace: repo.namespace,
        name: verdict.symbol,
        include_body: false,
        include_history: false,
        include_examples: false,
        include_lessons: false,
      },
      { timeoutMs: 2500 },
    );
    const dirtyFiles = files.filter((f) => {
      const rel = relPath(repo.root, path.resolve(cwd, f));
      return rel != null && gitStatusClass(repo.root, rel) !== "";
    });
    const localGrep = dirtyFiles.length
      ? localGrepText(runLocalGrep(cwd, ctx.pattern, dirtyFiles), { pattern: ctx.pattern, files: dirtyFiles })
      : "";
    const answer = symbolAnswerText(res, { asOf: repo.asOf, namespace: repo.namespace, localGrep, dirtyFiles });
    if (answer) verdict.deny = denySymbolGrepWithAnswer({ ...ctx, symbol: verdict.symbol }, answer);
  } catch {
    /* the plain redirect stands */
  }
  return verdict;
}

/** A GitHub issue search is a question the tracker record
 *  answers — one bounded tracker.duplicate_check on this checkout's namespace
 *  (the server binds it to the tracker namespace it may read).
 *  Denied once per query with the verdict and the closest items; the same
 *  command passes on the next try, and anything the record cannot answer
 *  (unbound, unauthorized, timeout) passes at once. */
async function settleTrackerSearch(repo, { search, sessionId, ctxBase }) {
  const key = `qa:${crypto.createHash("sha1").update(String(search.query)).digest("hex").slice(0, 16)}`;
  const counts = readCapCounts(sessionId);
  const verdict = { deny: null, reason: "tracker_search_repeat" };
  if (!capBudgetSpent(counts, key, TRACKER_SEARCH_DENIALS_PER_QUERY)) {
    verdict.reason = "tracker_unanswered";
    try {
      // `gh … --repo ClickHouse/ClickHouse` from a loom
      // checkout asks ClickHouse's tracker; the check and the hint name the
      // namespace of the repository the command names. One it cannot read or
      // that is not indexed is not this record's question: gh runs.
      let target = repo;
      const other = otherSearchRepo(search, repo.repo);
      if (other) {
        const repos = await postBounded(
          "code.repos",
          { consumer: "claude-code-shunt", repo: other },
          { timeoutMs: TRACKER_SEARCH_TIMEOUT_MS },
        );
        const hit = repoLookupOutcome(repos, other).hit;
        target = hit ? { ...repo, org: hit.org, namespace: hit.namespace } : null;
      }
      const res =
        target &&
        (await postBounded(
          "tracker.duplicate_check",
          {
            org: target.org || "",
            namespace: target.namespace,
            title: redactSecrets(search.query),
            frames: search.frames || [],
            top_k: 5,
          },
          { timeoutMs: TRACKER_SEARCH_TIMEOUT_MS },
        ));
      const answer = target ? trackerAnswerText(res, { query: search.query, namespace: target.namespace }) : "";
      if (answer && writeCapCounts(sessionId, spendCapBudget(counts, key))) {
        verdict.deny = denyGhSearchWithAnswer({ ...ctxBase, namespace: target.namespace }, answer);
      } else if (answer) {
        verdict.reason = "tracker_search_repeat";
      }
    } catch {
      /* the search runs */
    }
  }
  settle(repo, { tool: "Bash:gh", verdict, tokens: 0, path: "" });
}

// The whole hook, network included, ends within the repository lookup's
// bound plus room for one answer; past it the tool call proceeds unjudged.
const DEADLINE_MS = resolveTimeoutMs() + 4000;

async function main() {
  if (!ENABLED) return;
  setTimeout(() => process.exit(0), DEADLINE_MS).unref();
  const payload = await readStdinJson();
  const tool = normalizeToolName(payload.tool_name || "");
  const input = payload.tool_input || {};
  const cwd = payload.cwd || process.cwd();
  const sessionId = payload.session_id || "nosession";
  PENDING_PATH = sessionStatePath("shunt-pending", sessionId, { ext: ".log" });
  if (!["Read", "Grep", "Bash"].includes(tool)) return;
  // The one opt-out is the developer's own — local-first
  // (LOOM_CODE_LOCAL_FIRST=on / the `local_first` option, off by default)
  // stands the guard down entirely, whatever the commits.
  if (localFirstOn()) return;

  const root = git(["rev-parse", "--show-toplevel"], cwd);
  if (!root) return;
  const repo = { ...(await resolveIndexedRepo(root)), root };
  // No freshness stand-down — neither commit distance nor an
  // overdue mirror fetch turns the shunt off; `asOf` rides
  // the deny text as information.
  if (!repo.indexed) return;
  // The deny text names this checkout's org/namespace.
  const ctxBase = {
    prefixes: repo.prefixes,
    asOf: repo.asOf,
    org: repo.org || "",
    namespace: repo.namespace || "",
  };

  if (tool === "Read") {
    const abs = path.resolve(cwd, String(input.file_path || ""));
    const rel = relPath(root, abs);
    if (rel == null) return;
    settleRead(repo, { tool, rel, abs, offset: input.offset, limit: input.limit, sessionId, ctxBase });
    return;
  }

  if (tool === "Grep" && SHUNT_GREP) {
    const abs = path.resolve(cwd, String(input.path || "."));
    const rel = relPath(root, abs);
    let isFile = false;
    try {
      isFile = fs.statSync(abs).isFile();
    } catch {
      /* missing path: let grep fail itself */ return;
    }
    // Claude Code's Grep lists matching files unless asked for the lines. A
    // one-file grep that asks for line numbers or context wants the lines:
    // as a file list it answers with the file's own name and costs a turn.
    if (isFile && rel != null && wantsLines(input)) {
      rewrite({ ...input, output_mode: "content" });
      settle(repo, { tool, verdict: { deny: null, reason: "file_grep_lines" }, tokens: 0, path: rel });
      return;
    }
    const outputMode = String(input.output_mode || "files_with_matches");
    const ctx = { ...ctxBase, rel, isFile, pattern: input.pattern, outputMode };
    const verdict = await answerSymbolDeny(classifySymbolGrep(ctx, sessionId), repo, ctx, {
      cwd,
      files: isFile ? [String(input.path)] : [],
    });
    // Grep output size is unknowable up front: tokens 0 either way.
    settle(repo, { tool, verdict, tokens: 0, path: rel || "" });
    return;
  }

  if (tool === "Bash" && SHUNT_TRACKER && repo.namespace) {
    const search = ghIssueSearchInCommand(commandText(input));
    if (search) {
      await settleTrackerSearch(repo, { search, sessionId, ctxBase });
      return;
    }
  }

  if (tool === "Bash" && SHUNT_BASH) {
    // Each bulk segment of a `;` / `&&` / `|` chain is judged in order; the
    // first deny ends the call (one deny per hook), passes are all counted.
    for (const bulk of parseBashBulkSegments(commandText(input))) {
      if (await settleBashBulk(repo, { bulk, cwd: path.resolve(cwd, bulk.cwd || "."), sessionId, ctxBase })) return;
    }
  }
}

/** One bulk-shaped Bash segment; true when it was denied. */
async function settleBashBulk(repo, { bulk, cwd, sessionId, ctxBase }) {
  const root = repo.root;
  if (bulk.kind === "read") {
    const abs = path.resolve(cwd, bulk.file);
    const rel = relPath(root, abs);
    if (rel == null) return false;
    // A `sed -n 'A,Bp'` / `head -n N` / `tail -n +A` /
    // `awk 'NR>=A && NR<=B'` slice carries its span, so the read-window
    // cap judges it exactly as Read(offset, limit); a whole-file `cat`,
    // `nl`, `tac`, `sed -n p` keeps the whole-file rule.
    const cmdName = bulk.cmd || "cat";
    return settleRead(repo, {
      tool: `Bash:${cmdName}`,
      rel,
      abs,
      sessionId,
      ctxBase,
      offset: bulk.offset,
      limit: bulk.limit,
      denyRewrite: (t) =>
        t
          .replace("Whole-file reads are", `\`${cmdName}\` of a whole file is`)
          .replace(/^loom-code-shunt: Read of /, `loom-code-shunt: \`${cmdName}\` of `),
    });
  }
  const abs = path.resolve(cwd, bulk.dir);
  const rel = relPath(root, abs);
  let isFile = false;
  try {
    isFile = fs.statSync(abs).isFile();
  } catch {
    return false;
  }
  const ctx = { ...ctxBase, rel, isFile, pattern: bulk.pattern };
  const verdict = await answerSymbolDeny(classifySymbolGrep(ctx, sessionId), repo, ctx, {
    cwd,
    files: bulk.files || [bulk.dir],
  });
  return settle(repo, { tool: "Bash:grep", verdict, tokens: 0, path: rel || "" });
}

main()
  .catch(() => {})
  .finally(() => scheduleExit(0));
