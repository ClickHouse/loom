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

/** The line that names the repository's memory tag, or "" without a repository:
 *  hook captures carry the tag, and a fact the model saves about the repository
 *  needs it to be recalled with them. */
export function repoTagLine(repo) {
  if (!repo || !repo.id) return "";
  return `Memory about this repository carries tags: ["repo:${repo.id}"].`;
}

/**
 * The SessionStart output, "" when the session has nothing of its own to hear:
 * first-session, server-version and duplicate-mount notices, the checkout's
 * line (its code namespace, then its memory tag), the memory primer under a
 * heading when context.pack rendered one, then the task, brief, collections
 * and recording lines. What holds in every session (how to route a lookup,
 * which tool does what) is in the MCP instructions and the tool descriptions,
 * and is not repeated here: the host re-reads this text on every request.
 */
export function renderPrimer({
  notices = [],
  project,
  repo,
  rendered = "",
  codeBlock = "",
  lines = [],
  recordingLine = "",
}) {
  const code = codeBlock.trim();
  const tag = repoTagLine(repo);
  // One line for the checkout when its code text is one line. Beside a longer
  // code block, or none, the tag is its own line under a prefix the server
  // knows as Loom's own words, so capture never reads it as a fact.
  const checkout =
    code && !code.includes("\n")
      ? [code, tag].filter(Boolean).join(" ")
      : [code, tag && `# Loom memory primer: ${tag}`].filter(Boolean).join("\n\n");
  const primer = rendered.trim();
  const memory = primer ? `# Loom memory primer — project: ${project}\n\n${recalledDataBlock(primer)}` : "";
  const parts = [...notices, checkout, memory, ...lines, recordingLine].filter(Boolean);
  return parts.length ? `${parts.join("\n\n")}\n` : "";
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

/** Whether the session's MCP entry is the Claude Code plugin's own, which
 *  Claude Code marks by running the plugin's hooks with CLAUDE_PLUGIN_ROOT.
 *  That entry's helper binds loom_code_* calls to the checkout's repository,
 *  its URL names the `plugin` profile, whose startup instructions say how to
 *  route a lookup, and its toolset is the plugin's `toolset` option. Any other
 *  wiring (Codex, a hand-wired client) mounts an entry these hooks cannot
 *  read, so its primer says all of it. */
export function ownsMcpEntry(env = process.env) {
  return Boolean(env.CLAUDE_PLUGIN_ROOT);
}

/** One line for the primer naming the code namespace every loom_code_* call
 *  from this checkout must carry. Without it the server
 *  answers from the deployment's default repository — the public index —
 *  which is wrong in every other checkout and silently so. "" when the
 *  checkout matched no readable repository. */
export function codeScopeLine(hit, { ownEntry = false } = {}) {
  if (!hit || !hit.org || !hit.namespace) return "";
  // The plugin's own entry sends the namespace in a header: the line is for
  // the session whose header lookup missed its bound.
  if (ownEntry) {
    const repo = hit.repo ? `${hit.repo}${hit.branch ? ` @ ${hit.branch}` : ""}` : "this checkout";
    return `# Loom code index: ${repo}; pass namespace "${hit.namespace}" on loom_code_* calls.`;
  }
  const where = hit.repo ? ` (${hit.repo}${hit.branch ? ` @ ${hit.branch}` : ""})` : "";
  return (
    `Code index scope${where}: pass namespace: "${hit.namespace}" on EVERY loom_code_* call ` +
    `from this checkout. MCP infers org "${hit.org}" from the token; HTTP/SDK callers also pass org. ` +
    `Without the namespace the server answers from its default repository, not this one.`
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

/** The one sentence the Claude Code plugin's primer adds about an index whose mirror has
 *  not been fetched for `code_fetch_stale_after_seconds`
 *  (`status.fetch_overdue`), "" for a fresh one. Information, as in
 *  `codeArbitration`: it names the fetch time and does not send the model to
 *  grep. */
export function staleMirrorLine(status) {
  if (!status || status.fetch_overdue !== true) return "";
  return (
    `Its mirror was last fetched at ${fetchedAt(status)}, so the index may be BEHIND this checkout: ` +
    "name the indexed commit when you report from it, and grep only files with your own uncommitted changes."
  );
}

function fetchedAt(status) {
  return (
    String(status.clone_fetched_at || "")
      .replace(/\.\d+/, "")
      .replace("+00:00", "Z") || "an unknown time"
  );
}

// The taught arbitration, for a wiring whose MCP entry this plugin does not
// mount (Codex, a hand-wired client): the Claude Code plugin's own entry names
// the `plugin` profile, whose startup instructions carry the routing. Injected
// only when the repo is actually indexed so it never confuses sessions on
// other projects.
const CODE_ARBITRATION =
  "# Loom code index: this repository is indexed (per-merge fresh).\n" +
  "- Use Loom first when indexed evidence is needed: loom_code_symbol for definitions, " +
  "loom_code_callers for callers, loom_code_search for concepts, loom_code_grep for exact text or errors, " +
  "loom_code_impact for a change's effects.\n" +
  "- If the needed tool is deferred, discover it with the client's tool search (ToolSearch in Claude Code). " +
  "A definition does not supply its callers; retrieve the missing evidence for the question.\n" +
  "- If a hook already supplied the answer, use it; do not repeat the lookup. Read only a returned span " +
  "to verify it. Skip retrieval for self-contained tasks.\n" +
  "- Explicit local-tool requests, uncommitted changes and unindexed files: use local grep/Read. " +
  "A code question quoting an identifier or error is still a code investigation. If Loom is " +
  "unavailable or abstains, use a targeted local lookup and name the limitation.\n" +
  "- The index is master; this checkout may be behind it. For unchanged files, report the index's path:line.\n";

/** The arbitration block for this session. When the entry's
 *  mirror has not been fetched for `code_fetch_stale_after_seconds`
 *  (`status.fetch_overdue`), the header and the last rule say
 *  so — as information: the index still answers, and the
 *  shunt does not stand down for it, so the rule names the fetch time
 *  instead of sending the model to grep. */
export function codeArbitration(status) {
  if (!status || status.fetch_overdue !== true) return CODE_ARBITRATION;
  const when = fetchedAt(status);
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
