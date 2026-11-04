// The session-start context.pack request.
//
// `context.pack` can render a `runs` section — one line per recent session of
// this identity (date, run status, goal, tool calls by kind with errors, files
// touched, digest) — the shortlist an agent reads before opening a session with
// memory.evidence or memory.digests. The section is opt-in on the server; this
// is the client that opts in, at session start, where the agent orients.
// LOOM_CONTEXT_RUNS=false leaves it out; LOOM_CONTEXT_RUNS_LIMIT bounds the rows.

import { envNumber, envSwitch } from "./loom-client.mjs";
import { recalledDataBlock } from "./prompt-recall.mjs";
import { sessionRunId } from "./run-hook.mjs";
import { blockKeyFor } from "./working-memory.mjs";

/** The recent-runs shortlist in the session-start primer, off unless
 *  LOOM_CONTEXT_RUNS=on: it changes every session (each one adds its own run), and the
 *  host sends the primer in one cached block with its own reminders, so a list that
 *  differs re-writes that whole block every session. The run tools read runs on demand. */
export function wantsRuns(env = process.env) {
  return envSwitch("LOOM_CONTEXT_RUNS", false, env);
}

export function runsLimit(env = process.env) {
  const n = envNumber("LOOM_CONTEXT_RUNS_LIMIT", 8, { max: 50, env });
  return n >= 1 ? Math.floor(n) : 8;
}

/** The body of the session-start pack. `runId` is the run hook's id for the
 *  session ("" when none). */
export function startPackBody(scope, payload, { tokenBudget = 1200, runId = "", env = process.env } = {}) {
  const sessionId = payload.session_id || "";
  const body = {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    query: `Resume work on ${scope.project}. Recent decisions, conventions, open issues, recurring errors.`,
    // The session's run: a pack that names the run also
    // carries the run's scratchpad, ahead of the session note.
    run_id: runId || "-",
    mode: "startup",
    token_budget: tokenBudget,
    top_k: 5,
    // Only this project's Stop-hook bookmark, not every
    // repository's the agent has worked in.
    session_block: blockKeyFor(scope.project),
    // The session's own id and the run hook's run id (sessionRunId), so
    // the primer's exposure row joins the session's judged outcome.
    ...(sessionId ? { session_id: String(sessionId), run_id: sessionRunId(sessionId) } : {}),
  };
  if (wantsRuns(env)) {
    body.include_runs = true;
    body.runs_limit = runsLimit(env);
    // Give the shortlist its own slice of the primer instead of letting it
    // compete with the working memory and profile for the default budget.
    body.section_token_budgets = { runs: 300 };
  }
  return body;
}

/**
 * The SessionStart output: first-session and duplicate-mount notices, then
 * (when context.pack rendered one) the memory primer under a heading, the
 * repository tag line, the code block, the task, brief and collections lines, and the
 * recording line, which is always present.
 */
export function renderPrimer({
  notices = [],
  project,
  repo,
  rendered = "",
  codeBlock = "",
  lines = [],
  recordingLine,
}) {
  const notice = notices.filter(Boolean).join("\n\n");
  const repoLine = repo
    ? `Repository ${repo.slug} — rows from this checkout carry the tag \`repo:${repo.id}\`; ` +
      `pass tags: ["repo:${repo.id}"] to loom_search to stay within it.\n\n`
    : "";
  const extra = lines
    .filter(Boolean)
    .map((l) => `${l}\n\n`)
    .join("");
  const primer = rendered.trim();
  const head = notice ? `${notice}\n\n` : "";
  const body = primer
    ? `# Loom memory primer — project: ${project}\n\n${repoLine}${recalledDataBlock(primer)}\n\n`
    : repoLine;
  return `${head}${body}${codeBlock}${extra}${recordingLine}\n`;
}

// Code-plane orientation. The checkout's repository is resolved by the
// lookup every hook shares (lib/repo-scope.mjs); these render what the
// primer says about it.

/** The primer line when the checkout matched its index
 *  through anything but `origin` (or a repository named by LOOM_CODE_REPO / `code_repo`): a fork
 *  resolved to its upstream, or the `upstream` / another remote matched.
 *  `entry` is the resolver's cache entry, `remotes` `checkoutRemotes()`.
 *  "" for an origin / override match. */
export function matchNoteLine(entry, remotes = []) {
  if (!entry || !entry.indexed) return "";
  const via = String(entry.matchedVia || "");
  if (!via || via === "origin") return "";
  if (via === "configured") {
    return `# Loom code index: the configured code_repo ${entry.repo} → ${entry.namespace}`;
  }
  const mine = (remotes || []).find((r) => r.slug === entry.checkoutSlug);
  const checkout = (mine && mine.name) || entry.checkoutSlug || "this checkout";
  if (via === "fork_of") {
    return `# Loom code index: ${checkout} is a fork of ${entry.repo} → ${entry.namespace}`;
  }
  const remote = via.startsWith("remote:") ? `the ${via.slice(7)} remote` : `the ${via} remote`;
  return `# Loom code index: matched ${remote} ${checkout} → ${entry.namespace}`;
}

/** The primer line when no candidate (nor a fork's upstream)
 *  is an indexed repository the caller reads. */
export function notIndexedLine(remotes = []) {
  const names = (remotes || []).map((r) => r.name || r.slug).filter(Boolean);
  const first = names[0] || "this repository";
  const rest = names.length > 1 ? ` (nor ${names.slice(1).join(", ")})` : "";
  return `# Loom code index: ${first}${rest} is not an indexed repository.\n\n`;
}

/** One line for the primer naming the code namespace every loom_code_* call
 *  from this checkout must carry. Without it the server
 *  answers from the deployment's default repository — the public index —
 *  which is wrong in every other checkout and silently so. "" when the
 *  checkout matched no readable repository. */
export function codeScopeLine(hit) {
  if (!hit || !hit.org || !hit.namespace) return "";
  const where = hit.repo ? ` (${hit.repo}${hit.branch ? ` @ ${hit.branch}` : ""})` : "";
  return (
    `Code index scope${where}: pass org: "${hit.org}", namespace: "${hit.namespace}" on EVERY ` +
    `loom_code_* call from this checkout — without them the server answers from its default ` +
    `repository, not this one.`
  );
}

/** The `code.index_status` body: the matched repository's namespace, or `{}`
 *  — the deployment default — when nothing matched. */
export function indexStatusBody(hit) {
  return hit && hit.org && hit.namespace ? { org: hit.org, namespace: hit.namespace } : {};
}

/** The checkout the hook payload names, falling back to this process's cwd.
 *  The code block (remote, branch, cache root) must come from the project the
 *  session opened, not from the directory node happened to start in. */
export function checkoutDir(payload, fallback = process.cwd()) {
  const cwd = payload && typeof payload.cwd === "string" ? payload.cwd.trim() : "";
  return cwd || fallback;
}

/** The session primer's code block in local-first mode: no repo map, no
 *  arbitration towards the index — the working tree is what this user wants
 *  searched, the guard stands down whatever the commits, and the index is named
 *  only for what it is. */
export function localFirstBlock(asOf, scopeLine = "") {
  const a = String(asOf || "").slice(0, 9) || "unknown";
  return (
    (scopeLine ? `${scopeLine}\n\n` : "") +
    "# Loom code index: LOCAL-FIRST mode (LOOM_CODE_LOCAL_FIRST=on).\n" +
    "- Search the working tree with grep/Read; the code guard stands down.\n" +
    `- loom_code_symbol / loom_code_callers / loom_code_search describe the indexed commit (${a}), not this tree; ` +
    "use them only when asked about upstream, and say which commit they describe.\n"
  );
}

// The taught arbitration — short, durable, injected only when the
// repo is actually indexed so it never confuses sessions on other projects.
const CODE_ARBITRATION =
  "# Loom code index: this repository is indexed (per-merge fresh).\n" +
  "- Grep or Read the names you know first. When grep misses or floods, or there is no name " +
  "to grep: loom_code_search. Every definition or caller of a symbol, exactly (C++ overloads " +
  "included): loom_code_symbol, loom_code_callers. Independent lookups go in one turn.\n" +
  "- Exact strings or your uncommitted changes: grep — the index knows merged code. " +
  "abstain: true means grep.\n" +
  "- A hit carries the unit's path, lines and code: answer from it; open a file to change it, " +
  "not to re-read a hit.\n" +
  "- Already reported? loom_tracker_duplicate_check. What introduced a bug? " +
  "loom_code_bisect_candidates. Which change made X behave like Y? git log -S / git blame on " +
  "the code that does it, or loom_code_pr_check with the behaviour as the title — not " +
  "loom_code_search, which finds code, not changes. Someone already on this PR? " +
  "loom_code_pr_check. What does my " +
  "diff break? loom_code_impact.\n" +
  "- The index is master; this checkout may be behind it. For files you have not modified, " +
  "report the index's path:line — do not re-read to reconcile line numbers.\n";

/** The arbitration block for this session. When the entry's
 *  mirror has not been fetched for `code_fetch_stale_after_seconds`
 *  (`status.fetch_overdue`), the header and the last rule say
 *  so — as information: the index still answers, and the
 *  shunt does not stand down for it, so the rule names the fetch time
 *  instead of sending the model to grep. */
export function codeArbitration(status) {
  if (!status || status.fetch_overdue !== true) return CODE_ARBITRATION;
  const when =
    String(status.clone_fetched_at || "")
      .replace(/\.\d+/, "")
      .replace("+00:00", "Z") || "an unknown time";
  return CODE_ARBITRATION.replace(
    "# Loom code index: this repository is indexed (per-merge fresh).\n",
    `# Loom code index: this repository is indexed, but its mirror was last fetched at ${when} — ` +
      "the index may be BEHIND this checkout.\n",
  ).replace(
    /- The index is master; this checkout may be behind it\.[^\n]*\n/,
    `- The index holds the code as of that fetch (${when}); its lag reads 0 only because nothing ` +
      "fetched. For files you have not modified, report the index's path:line and name its commit; " +
      "grep only files with your own uncommitted changes.\n",
  );
}
