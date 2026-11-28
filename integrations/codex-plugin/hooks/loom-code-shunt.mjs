#!/usr/bin/env node
// loom-code-shunt — PreToolUse (Read | Grep | Bash): redirect the bulk-read
// reflex (whole-file Read, tree-wide Grep, `cat`/`rg`/`sed -n` in Bash)
// inside a loom-indexed repository to the code ops, and answer a GitHub
// issue search from the tracker record. Policy: lib/code-shunt.mjs.
//
// Input: the host's PreToolUse JSON on stdin (tool_name, tool_input, cwd,
// session_id). Output: nothing (the call proceeds) or one `deny` whose
// reason carries the index's answer: the declarations of the lines a read
// asked for, a symbol's definition, the tracker's verdict. Nothing else ever
// blocks.
//
// Fail-open: no token, no repository or an unindexed one, a lookup the index
// did not answer in full, a modified or untracked file, a targeted read, a
// Bash command of several parts → the call proceeds.
// Bounds: the repository lookup (cached 10 min, 1 min when unanswered) has
// LOOM_SHUNT_RESOLVE_TIMEOUT_MS (8 s); a symbol or outline answer 2.5 s; a
// tracker answer LOOM_TRACKER_SHUNT_TIMEOUT_MS (3 s); the whole process exits
// by DEADLINE_MS.
//
// Side effects, all in $TMPDIR: the shared repository cache
// (lib/repo-scope.mjs); per-session denial budgets for the read-window cap,
// symbol greps and tracker searches, and the whole files whose lookup gave no
// declarations (sessionStatePath "shunt-cap"); the
// user's decision log (tmpStatePath "shunt-log") and its unflushed copy, one
// batch per reporting scope and session (repo.pending, named by batchKind in
// lib/shunt-ledger.mjs), all through lib/tmp-state.mjs's private-file helpers,
// flushed as code.outcome rows (fire-and-forget, 300 ms) on a deny or every
// SHUNT_FLUSH_EVERY lines (lib/shunt-ledger.mjs).
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import {
  git,
  gitToplevel,
  pathUnder,
  planeEnabled,
  postBounded,
  postFireAndForget,
  readStdinJson,
  resolveServerUrl,
  resolveToken,
  redactSecrets,
  scheduleExit,
  commandText,
  normalizeToolName,
  envSwitch,
} from "../lib/loom-client.mjs";
import {
  DEFAULT_MAX_LINES,
  DEFAULT_OUTLINE_BUDGET,
  DEFAULT_READ_CAP,
  SYMBOL_GREP_DENIALS_PER_SYMBOL,
  TRACKER_SEARCH_DENIALS_PER_QUERY,
  capBudgetSpent,
  classifyGrep,
  wantsLines,
  classifyRead,
  declarationsInSpan,
  denyGhSearchWithAnswer,
  denySymbolGrepWithAnswer,
  localFirstOn,
  localGrepText,
  otherSearchRepo,
  readSpan,
  runLocalGrep,
  spendCapBudget,
  symbolInPattern,
  trackerAnswerText,
  wholeSymbolAnswer,
} from "../lib/code-shunt.mjs";
import { ghIssueSearchInCommand, isCompoundCommand, parseBashBulkSegments } from "../lib/shell-command.mjs";
import {
  aggregateShuntLog,
  batchKind,
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

const ENABLED = planeEnabled("code") && envSwitch("LOOM_CODE_SHUNT", true);
const MAX_LINES = Number(process.env.LOOM_SHUNT_MAX_LINES || DEFAULT_MAX_LINES);
const READ_CAP = Math.max(0, Number(process.env.LOOM_SHUNT_READ_CAP || DEFAULT_READ_CAP) || 0);
// The deny text stops naming its own bypass unless asked to.
const SHUNT_GREP = envSwitch("LOOM_SHUNT_GREP", true);
const SHUNT_BASH = envSwitch("LOOM_SHUNT_BASH", true);
// `gh issue list --search` / `gh search issues` inside an
// indexed checkout is answered by the tracker record first. `LOOM_TRACKER_SHUNT=off`.
// It asks tracker.duplicate_check, which is no code op, and names a tool the
// listing of code tools alone leaves out: a session with both planes only.
const SHUNT_TRACKER = planeEnabled("memory") && envSwitch("LOOM_TRACKER_SHUNT", true);
const TRACKER_SEARCH_TIMEOUT_MS = Math.max(500, Number(process.env.LOOM_TRACKER_SHUNT_TIMEOUT_MS || 3000) || 3000);
// One symbol or outline answer, asked only for a call the local rules would
// refuse. A call it does not answer runs having waited for it: once per symbol
// or whole file per session, at each over-cap read.
const LOOKUP_TIMEOUT_MS = 2500;
const LOG_ON = envSwitch("LOOM_SHUNT_LOG", true);
const REPORT_ON = envSwitch("LOOM_SHUNT_REPORT", true);
const FLUSH_TIMEOUT_MS = 300;
const UID = typeof process.getuid === "function" ? process.getuid() : "";
// One decision log per user.
const LOG_PATH = tmpStatePath("shunt-log", UID, { ext: ".log" });

/** The unflushed batch, set on `repo` in main: `kind` names the reporting scope
 *  (server, token, repository) every batch of it shares, `path` this session's
 *  batch, a name nobody can create ahead of the session. Batches and claimed
 *  batches are swept with the other state files. A flush also claims the
 *  scope's other batches: those ended or crashed sessions left, and those of
 *  sessions still running. */
function pendingFor(repo, sessionId) {
  const kind = batchKind({
    serverUrl: resolveServerUrl(),
    token: resolveToken(),
    org: repo.org || "",
    namespace: repo.namespace || "",
  });
  return { kind, path: sessionStatePath(kind, sessionId, { ext: ".log" }) };
}

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

/** Per-session counts, {key: n}: three denial budgets, over-cap reads (the
 *  file's path), symbol greps (`sym:`) and answered tracker searches (`qa:`),
 *  and the whole files whose lookup gave no declarations (`miss:`), asked
 *  about once.
 *  {} when the file is missing or refused. A denial is given only when its
 *  spend was written: when the file cannot be kept (tmp not writable, or a file
 *  planted there) the call passes as if its budget were spent, so a planted
 *  file cannot lock the agent out. */
function readCapCounts(sessionId) {
  return readSessionState("shunt-cap", sessionId);
}

// The pass reason for a call whose budget spend could not be written: it passes.
const BUDGET_UNRECORDED = "budget_unrecorded";
// A Bash command of several parts is never refused: a refusal withholds every
// part. Its reads and greps are still counted as passes.
const COMPOUND_COMMAND = { deny: null, reason: "compound_command" };
// The index did not answer in full, so there is nothing to refuse with.
const INDEX_UNANSWERED = { deny: null, reason: "index_unanswered" };

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
function logDecision(pending, { decision, tool, tokensSaved, reason, passReason, path, startLine, endLine }) {
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
    writePrivateFile(pending.path, line, { append: true });
    const text = readPrivateFile(pending.path);
    return text.split("\n").length - 1;
  } catch {
    return 0;
  }
}

/** The batch files of this reporting scope that this user owns: this
 *  session's and any other session's. Another user's file, a link or a hard
 *  link is left alone. */
function scopeBatches(pending) {
  const dir = path.dirname(pending.path);
  const prefix = `loom-${pending.kind}-`;
  let names = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  return names
    .filter((n) => n.startsWith(prefix) && n.endsWith(".log"))
    .map((n) => path.join(dir, n))
    .filter((file) => {
      try {
        const st = fs.lstatSync(file);
        return st.isFile() && st.nlink === 1 && (UID === "" || st.uid === UID);
      } catch {
        return false;
      }
    });
}

/** Claim one batch by renaming it to a name of this process, read it with the
 *  private-file checks, and remove it: of two hooks flushing at once only one
 *  gets each batch. "" when another hook claimed it first, or it is not ours. */
function claimBatch(file) {
  const claimed = tmpStatePath("shunt-claim", `${file}:${process.pid}:${crypto.randomBytes(8).toString("hex")}`, {
    ext: ".log",
  });
  try {
    fs.renameSync(file, claimed);
  } catch {
    return "";
  }
  let text = "";
  try {
    text = readPrivateFile(claimed);
  } catch {
    /* replaced between the check and the rename: not ours to post */
  }
  removeFile(claimed);
  return text;
}

/** Ship the scope's pending batches as code.outcome rows. Fire-and-forget with
 *  a 300 ms ceiling; a lost batch costs a few counter ticks. Each batch is
 *  claimed before it is read, so it is posted at most once. */
function flushPending(repo) {
  if (!REPORT_ON) return;
  const text = scopeBatches(repo.pending).map(claimBatch).join("");
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
  const unflushed = logDecision(repo.pending, {
    decision: denied ? "hit" : "pass",
    tool,
    tokensSaved: tokens,
    reason: verdict.deny || "",
    passReason: denied ? "" : verdict.reason,
    path,
    startLine: span.start,
    endLine: span.end,
  });
  if (shouldFlushShunt({ denied, pending: unflushed })) flushPending(repo);
}

/** The index's declarations in `span` of `rel`: one bounded code.outline. []
 *  when it did not answer for the whole span. */
async function indexedDeclarations(repo, rel, span) {
  if (!repo.namespace) return [];
  const outline = await postBounded(
    "code.outline",
    {
      org: repo.org || "",
      namespace: repo.namespace,
      path: rel,
      token_budget: DEFAULT_OUTLINE_BUDGET,
      include_lessons: false,
      consumer: "claude-code-shunt",
    },
    { timeoutMs: LOOKUP_TIMEOUT_MS },
  );
  return declarationsInSpan(outline, span);
}

/** The declarations a refusal of this read would list, [] when the index does
 *  not hold them all. A whole file (`missKey` set) is asked about once per
 *  session: a lookup that gave none, whether the index answered with an empty
 *  or cut outline or did not answer, is kept in the counts file under that
 *  key, so an unreachable server costs each file one wait. The counts are read
 *  again for the write: another hook of the session may have spent a budget
 *  during the lookup. */
async function readDeclarations(repo, rel, span, sessionId, missKey) {
  if (missKey && capBudgetSpent(readCapCounts(sessionId), missKey, 1)) return [];
  const declarations = await indexedDeclarations(repo, rel, span);
  if (missKey && !declarations.length) writeCapCounts(sessionId, spendCapBudget(readCapCounts(sessionId), missKey));
  return declarations;
}

/** Read-shaped decision (Read tool or `cat`): classify, price, span. A
 *  ranged read consults the session's cap budget first; a cap denial spends
 *  one of the file's denials. A denial carries the declarations of the lines
 *  asked for; without them, or inside a `compound` command, the read passes.
 *  `fromEnd`: the slice is the file's last `limit` lines (`tail -n N`). */
async function settleRead(repo, opts) {
  const { tool, rel, abs, limit, fromEnd, sessionId, ctxBase, denyRewrite, compound } = opts;
  const lineCount = countLines(abs);
  const offset = fromEnd ? Math.max(1, lineCount - limit + 1) : opts.offset;
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
  const span = readSpan({ lineCount, offset, limit });
  let verdict = classifyRead(readCtx, MAX_LINES);
  if (verdict.deny && compound) verdict = COMPOUND_COMMAND;
  else if (verdict.deny) {
    const asked = ranged ? span : { start: 1, end: lineCount };
    const declarations = await readDeclarations(repo, rel, asked, sessionId, ranged ? "" : `miss:${rel}`);
    verdict = declarations.length ? classifyRead({ ...readCtx, declarations }, MAX_LINES) : INDEX_UNANSWERED;
  }
  if (verdict.capped && !writeCapCounts(sessionId, spendCapBudget(readCapCounts(sessionId), rel)))
    verdict = { deny: null, reason: BUDGET_UNRECORDED };
  if (verdict.deny && denyRewrite) verdict.deny = denyRewrite(verdict.deny);
  // What the read lets into the context: the whole file, or the span of a
  // ranged read. A hit kept that out, less the refusal's own text.
  const read = estimateTokensRead({ bytes, lineCount, offset, limit });
  const tokens = verdict.deny ? Math.max(0, read - estimateTokensSaved(verdict.deny.length)) : read;
  settle(repo, { tool, verdict, tokens, path: rel, span });
}

/** classifyGrep with the symbol rule's once-per-symbol budget:
 *  the budget shares the read-cap counts file under a `sym:` key. Inside a
 *  `compound` command the grep that would be a symbol lookup passes as one,
 *  and spends nothing. */
function classifySymbolGrep(ctx, sessionId, compound = false) {
  const symbol = ctx.isFile ? symbolInPattern(ctx.pattern) : "";
  if (!symbol) return classifyGrep(ctx);
  const key = `sym:${symbol}`;
  const counts = readCapCounts(sessionId);
  const verdict = classifyGrep({
    ...ctx,
    symbol,
    symbolBudgetSpent: capBudgetSpent(counts, key, SYMBOL_GREP_DENIALS_PER_SYMBOL),
  });
  if (verdict.symbol && compound) return COMPOUND_COMMAND;
  if (verdict.symbol && !writeCapCounts(sessionId, spendCapBudget(counts, key)))
    return { deny: null, reason: BUDGET_UNRECORDED };
  return verdict;
}

/** A symbol-grep denial carries the definition: one bounded code.symbol call,
 *  so the turn is one model round, not three (denied grep, loom_code_symbol,
 *  confirming grep). The index answer is the answer, whatever commit the
 *  checkout is on; named files the caller modified get their local grep
 *  inlined. When the index holds no single definition, does not answer in
 *  time, or those lines do not fit, the grep runs. */
async function answerSymbolDeny(verdict, repo, ctx, { cwd = repo.root, files = [] } = {}) {
  if (!verdict.symbol) return verdict;
  if (!repo.namespace) return INDEX_UNANSWERED;
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
    { timeoutMs: LOOKUP_TIMEOUT_MS },
  );
  const dirtyFiles = files.filter((f) => {
    const rel = pathUnder(repo.root, path.resolve(cwd, f));
    return rel != null && gitStatusClass(repo.root, rel) !== "";
  });
  const localGrep = dirtyFiles.length
    ? localGrepText(runLocalGrep(cwd, ctx.pattern, dirtyFiles), { pattern: ctx.pattern, files: dirtyFiles })
    : "";
  const answer = wholeSymbolAnswer(res, { asOf: repo.asOf, namespace: repo.namespace, localGrep, dirtyFiles });
  if (!answer) return INDEX_UNANSWERED;
  return { ...verdict, deny: denySymbolGrepWithAnswer({ ...ctx, symbol: verdict.symbol }, answer) };
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
      if (answer) {
        if (writeCapCounts(sessionId, spendCapBudget(counts, key))) {
          verdict.deny = denyGhSearchWithAnswer({ ...ctxBase, namespace: target.namespace }, answer);
        } else {
          verdict.reason = BUDGET_UNRECORDED;
        }
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
  if (!["Read", "Grep", "Bash"].includes(tool)) return;
  // The one opt-out is the developer's own — local-first
  // (LOOM_CODE_LOCAL_FIRST=on / the `local_first` option, off by default)
  // stands the guard down entirely, whatever the commits.
  if (localFirstOn()) return;

  const root = gitToplevel(cwd);
  if (!root) return;
  const repo = { ...(await resolveIndexedRepo(root)), root };
  // No freshness stand-down — neither commit distance nor an
  // overdue mirror fetch turns the shunt off; `asOf` rides
  // the deny text as information.
  if (!repo.indexed) return;
  repo.pending = pendingFor(repo, sessionId);
  // The deny text names this checkout's org/namespace.
  const ctxBase = {
    prefixes: repo.prefixes,
    asOf: repo.asOf,
    org: repo.org || "",
    namespace: repo.namespace || "",
  };

  if (tool === "Read") {
    const abs = path.resolve(cwd, String(input.file_path || ""));
    const rel = pathUnder(root, abs);
    if (rel == null) return;
    await settleRead(repo, { tool, rel, abs, offset: input.offset, limit: input.limit, sessionId, ctxBase });
    return;
  }

  if (tool === "Grep" && SHUNT_GREP) {
    const abs = path.resolve(cwd, String(input.path || "."));
    const rel = pathUnder(root, abs);
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
    const ctx = {
      ...ctxBase,
      rel,
      isFile,
      pattern: input.pattern,
      outputMode,
      modified: isFile && rel != null ? gitStatusClass(root, rel) : "",
    };
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
    // Each bulk segment of a `;` / `&&` / `|` chain is counted in order; only
    // a command that is one read or one grep can be denied.
    const command = commandText(input);
    const compound = isCompoundCommand(command);
    for (const bulk of parseBashBulkSegments(command)) {
      await settleBashBulk(repo, { bulk, cwd: path.resolve(cwd, bulk.cwd || "."), sessionId, ctxBase, compound });
    }
  }
}

/** One bulk-shaped Bash segment. Inside a `compound` command it passes: no
 *  lookup is made and no denial budget is spent. */
async function settleBashBulk(repo, { bulk, cwd, sessionId, ctxBase, compound }) {
  const root = repo.root;
  if (bulk.kind === "read") {
    const abs = path.resolve(cwd, bulk.file);
    const rel = pathUnder(root, abs);
    if (rel == null) return;
    // A `sed -n 'A,Bp'` / `head -n N` / `tail -n +A` /
    // `awk 'NR>=A && NR<=B'` slice carries its span, so the read-window
    // cap judges it exactly as Read(offset, limit); `tail -n N` carries a
    // length alone, counted back from the file's last line; a whole-file
    // `cat`, `nl`, `tac`, `sed -n p` keeps the whole-file rule.
    const cmdName = bulk.cmd || "cat";
    await settleRead(repo, {
      tool: `Bash:${cmdName}`,
      rel,
      abs,
      sessionId,
      ctxBase,
      compound,
      offset: bulk.offset,
      limit: bulk.limit,
      fromEnd: bulk.offset == null && bulk.limit != null,
      denyRewrite: (t) => t.replace(/^loom-code-shunt: Read of /, `loom-code-shunt: \`${cmdName}\` of `),
    });
    return;
  }
  const abs = path.resolve(cwd, bulk.dir);
  const rel = pathUnder(root, abs);
  let isFile = false;
  try {
    isFile = fs.statSync(abs).isFile();
  } catch {
    return;
  }
  const ctx = {
    ...ctxBase,
    rel,
    isFile,
    pattern: bulk.pattern,
    modified: isFile && rel != null ? gitStatusClass(root, rel) : "",
  };
  const verdict = await answerSymbolDeny(classifySymbolGrep(ctx, sessionId, compound), repo, ctx, {
    cwd,
    files: bulk.files || [bulk.dir],
  });
  settle(repo, { tool: "Bash:grep", verdict, tokens: 0, path: rel || "" });
}

main()
  .catch(() => {})
  .finally(() => scheduleExit(0));
