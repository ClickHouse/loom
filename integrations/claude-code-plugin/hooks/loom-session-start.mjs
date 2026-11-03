#!/usr/bin/env node
// SessionStart primer: one token-budgeted context block per session,
// printed to stdout, which the host injects as context. Never per turn.
//
// The block is `context.pack` (a curated, sectioned primer: pinned slots,
// profile, relevant memories, procedures, recent runs), plus the code-index
// orientation for an indexed checkout, the task inbox line, the recording
// line and first-session notices. Before any of it the hook materializes an
// install-time token (lib/plugin-options.mjs) and, with no credential at all,
// signs the machine up (lib/first-session.mjs).
//
// A hung server degrades to no primer; the unref'd exit timer bounds the
// rest. LOOM_INJECT_CONTEXT=false turns the primer off.

import {
  agentMode,
  envNumber,
  envSwitch,
  gitToplevel,
  hookClient,
  measureRoundTrip,
  postBounded,
  readStdinJson,
  recordRoundTrip,
  requestTimeoutMs,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { ensureCredential, renderFirstSessionNotice } from "../lib/first-session.mjs";
import { duplicateNotice } from "../lib/mcp-duplicate.mjs";
import { localFirstOn } from "../lib/code-shunt.mjs";
import { sessionRunId } from "../lib/run-hook.mjs";
import {
  checkoutDir,
  codeArbitration,
  codeScopeLine,
  indexStatusBody,
  localFirstBlock,
  renderPrimer,
  matchNoteLine,
  notIndexedLine,
  startPackBody,
} from "../lib/session-start.mjs";
import { checkoutRemotes, lookupIndexedRepo } from "../lib/repo-scope.mjs";
import { readRecording, recordingFromNamespace, recordingPrimerLine, writeRecording } from "../lib/recording.mjs";
import { teamRulesLine } from "../lib/file-context.mjs";
import { applyPluginOptions, installTokenNotice, saveInstallServerUrl } from "../lib/plugin-options.mjs";
import { cacheIsFresh, countsBody, renderTaskLine, summarizeCounts } from "../lib/task-inbox.mjs";
import { briefPrimerEnabled, primerLine } from "../lib/session-brief.mjs";
import { readJsonFile, sweepStaleStateFiles, tmpStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

async function main() {
  // A token given at `claude plugin install --config token=…`
  // reaches the hooks as an environment variable but never the MCP header
  // helper; write it once to ~/.loom/config.json, where the helper (and the
  // CLI, the SDK) read it. Before the token check below, so this very
  // session's hooks use it too.
  const optionPlan = applyPluginOptions();
  // The notice is about Claude Code's MCP entry (Codex's is fixed in its
  // plugin). An agent signs in as itself, so the config token is not what
  // its tools send.
  const optionNotice = hookClient() === "claude-code" && !agentMode() ? installTokenNotice(optionPlan) : "";
  saveInstallServerUrl();
  sweepStaleStateFiles();
  // Default ON; opt out with LOOM_INJECT_CONTEXT=false (see header).
  if (!envSwitch("LOOM_INJECT_CONTEXT", true)) {
    scheduleExit(0);
    return;
  }

  let token = resolveToken();
  let firstSessionNotice = "";
  if (!token) {
    // The first session signs itself up instead of going
    // silent. The token lands in ~/.loom/config.json for every later hook;
    // this process keeps using it through LOOM_API_KEY.
    const signup = await ensureCredential({ hasToken: false, client: hookClient(), serverUrl: resolveServerUrl() });
    firstSessionNotice = renderFirstSessionNotice(signup, resolveServerUrl());
    if (signup.status === "created") {
      process.env.LOOM_API_KEY = signup.token;
      token = signup.token;
    } else {
      if (firstSessionNotice) process.stdout.write(`${firstSessionNotice}\n`);
      scheduleExit(0);
      return;
    }
  }

  const payload = await readStdinJson();
  const scope = resolveScope(payload.cwd);
  const tokenBudget = Math.round(envNumber("LOOM_CONTEXT_TOKEN_BUDGET", 1200, { min: 100 }));

  // One GET /v1/health round trip, measured beside the primer and cached per
  // server, sets every later hook's request bound.
  const serverUrl = resolveServerUrl();
  measureRoundTrip(serverUrl)
    .then((ms) => {
      if (ms) recordRoundTrip(serverUrl, ms);
    })
    .catch(() => {});

  // The primer's parts are independent bounded reads: run them side by side,
  // so a hung server costs the session one request bound, not one per part.
  const [result, codeBlock, taskLine, briefsLine, recording] = await Promise.all([
    postBounded(
      "context.pack",
      startPackBody(scope, payload, { tokenBudget, runId: sessionRunId(payload.session_id) || "" }),
      { token, serverUrl },
    ),
    // Code-plane orientation for an indexed checkout (LOOM_CODE_MAP=false
    // drops it; the map's budget is additive to the primer's).
    envSwitch("LOOM_CODE_MAP", true) ? buildCodeBlock(scope, token, checkoutDir(payload)) : "",
    envSwitch("LOOM_TASK_INBOX_LINE", true) ? taskInboxLine(scope, token) : "",
    briefPrimerEnabled() ? savedBriefsLine(scope, token) : "",
    recordingState(scope, token),
  ]);

  // A user-scope MCP entry duplicating this plugin's mount.
  const dup = hookClient() === "claude-code" ? duplicateNotice({ serverUrl }) : "";
  process.stdout.write(
    renderPrimer({
      notices: [firstSessionNotice, dup, optionNotice],
      project: scope.project,
      repo: scope.repo,
      rendered: result && typeof result.rendered === "string" ? result.rendered : "",
      codeBlock,
      lines: [taskLine, briefsLine],
      // The model must not log turns itself: the Stop hook does, when the
      // owner's Recording switch is on. Always said, even with no primer.
      recordingLine: recordingPrimerLine(recording, scope.namespace),
    }),
  );
  scheduleExit(0);
}

/** The Recording switch for this scope: the flusher's
 *  cached verdict when fresh, else one bounded `namespace.get` (cached the
 *  same way); null when neither answers. */
async function recordingState(scope, token) {
  if (!scope || !scope.org || !scope.namespace) return null;
  const serverUrl = resolveServerUrl();
  const cached = readRecording(serverUrl, scope.org, scope.namespace);
  if (cached !== null) return cached;
  const body = await postBounded("namespace.get", { org: scope.org, namespace: scope.namespace }, { token, serverUrl });
  const on = recordingFromNamespace(body);
  writeRecording(serverUrl, scope.org, scope.namespace, on);
  return on;
}

/** Where the task inbox line for a scope is cached: one file per (server, org, namespace). */
function taskLineCachePath(scope) {
  return tmpStatePath("taskq", `${resolveServerUrl()}|${scope.org}|${scope.namespace}`);
}

/** The task inbox line for this scope: the cached one when
 *  younger than a minute, else one bounded `task.counts`. "" on any error, an
 *  op the deployment does not serve (404), a token without the read (403), or
 *  an empty board — the result is cached too, so a failing call is repeated
 *  once a minute at most. */
async function taskInboxLine(scope, token) {
  if (!scope || !scope.org || !scope.namespace) return "";
  const cp = taskLineCachePath(scope);
  const cached = readJsonFile(cp, null);
  if (cacheIsFresh(cached)) return cached.line;
  const result = await postBounded("task.counts", countsBody(scope), { token, serverUrl: resolveServerUrl() });
  const line = renderTaskLine(summarizeCounts(result));
  writeJsonFile(cp, { ts: Date.now(), line });
  return line;
}

/** One line naming this project's saved session briefs, so a
 *  session that starts after a compaction, a resume or a break knows it can
 *  load where the work stood. Never the brief itself — it is loaded on
 *  request (the loom-session skill), and a pointer costs ~60 tokens. "" on
 *  any error, an op the deployment does not serve (404) or no briefs. */
async function savedBriefsLine(scope, token) {
  if (!scope || !scope.org || !scope.namespace || !scope.project) return "";
  const result = await postBounded(
    "session.brief_list",
    { org: scope.org, namespace: scope.namespace, agent: scope.agent, project: scope.project, limit: 3 },
    { token, serverUrl: resolveServerUrl() },
  );
  return primerLine(result && Array.isArray(result.briefs) ? result.briefs : []);
}

async function buildCodeBlock(scope, token, cwd) {
  const serverUrl = resolveServerUrl();
  // `cwd` is the payload's checkout (checkoutDir), never this process's own
  // directory: the hook's process.cwd() is the plugin's, not the project's.
  const root = gitToplevel(cwd);
  // The checkout's candidates — origin, upstream, the other
  // remotes (or the repository LOOM_CODE_REPO / `code_repo` names) — resolved by the one lookup every hook
  // shares (lib/repo-scope.mjs), which also fills the cache the shunt and the
  // MCP headers helper read. A fork resolves to its
  // upstream's index. When the block cannot be built, say
  // why in one line.
  // A repository named by LOOM_CODE_REPO / the `code_repo` option stands in
  // for the checkout's remotes, and works outside any checkout.
  const remotes = checkoutRemotes(root);
  if (!remotes.length) {
    return root
      ? "# Loom code index: this checkout has no git remote, so no index was looked up.\n\n"
      : "# Loom code index: not inside a git checkout; set LOOM_CODE_REPO=<owner>/<name> " +
          "(or the plugin's code_repo option, or code_repo in ~/.loom/config.json) to use a repository's index.\n\n";
  }
  const post = (op, body, o) => postBounded(op, body, { ...o, serverUrl });
  const { entry, status } = await lookupIndexedRepo(root, {
    token,
    post,
    consumer: "claude-code-session-start",
    timeoutMs: requestTimeoutMs(),
  });
  if (entry.unresolved) {
    return (
      "# Loom code index: the repository lookup did not answer within the hook's bound; " +
      "loom_code_* tools still work — pass namespace explicitly.\n\n"
    );
  }
  if (!entry.indexed) return notIndexedLine(remotes);
  const hit = {
    org: entry.org,
    namespace: entry.namespace,
    repo: entry.repo,
    branch: entry.branch,
    git_head: entry.asOf,
  };
  // The scope line rides whenever the checkout matched a readable repository,
  // even if index_status is unreachable: the namespace is the fact the model
  // needs on every direct loom_code_* call.
  // A match through anything but origin says so first.
  const matchNote = matchNoteLine(entry, remotes);
  const scopeLine = [matchNote, codeScopeLine(hit), teamRulesLine(hit)].filter(Boolean).join("\n");
  const repo = status && typeof status.repo === "string" ? status.repo : "";
  if (!repo || !status.git_head) return scopeLine ? `${scopeLine}\n\n` : "";
  // Local-first mode names the situation and
  // skips the map — the working tree is what this user wants searched, and
  // the guard stands down whatever the commits.
  if (localFirstOn()) return localFirstBlock(status.git_head, scopeLine) + "\n";
  const repoName = repo.split("/").pop().toLowerCase();
  // Match on the project directory name OR the matched repository's basename
  // (a fork or an upstream remote matched it).
  const remoteName = String(entry.repo || "")
    .split("/")
    .pop()
    .toLowerCase();
  const project = (scope.project || "").toLowerCase();
  if (remoteName !== repoName && project !== repoName) return scopeLine ? `${scopeLine}\n\n` : "";
  // The repo map is off unless LOOM_CODE_MAP_TOKEN_BUDGET names a budget (100 or more):
  // the primer is re-read on every turn of the session, and an agent locates code with
  // code.search, not with a hub list.
  const budget = Math.round(envNumber("LOOM_CODE_MAP_TOKEN_BUDGET", 0, { min: 0 }));
  // The map must come from the same repository as the status.
  const map =
    budget >= 100
      ? await postBounded("code.map", { token_budget: budget, ...indexStatusBody(hit) }, { token, serverUrl })
      : null;
  const mapText = map && typeof map.map === "string" ? map.map.trim() : "";
  const arbitration = codeArbitration(status); // Says so when the mirror stalled
  const head = scopeLine ? `${scopeLine}\n\n${arbitration}` : arbitration;
  if (!mapText) return head + "\n";
  return `${head}\n${mapText}\n\n`;
}

runHookMain(main);
