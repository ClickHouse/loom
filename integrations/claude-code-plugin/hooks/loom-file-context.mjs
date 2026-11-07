#!/usr/bin/env node
// loom-file-context — PreToolUse (Edit|Write|MultiEdit|NotebookEdit).
//
// Before the agent edits a file, ask Loom what it knows about THAT file
// (`memory.for_file`: memories whose files[] facet names it) and hand the
// rendered block to the model as additionalContext.
// A constraint that arrives after the edit is a review comment; before the
// edit it is a constraint. The agent does not have to remember to search.
//
// Posture matches the other hooks: fail-open, one bounded request (3 s
// abort), never a permission decision — this hook only ADDS context. One
// lookup per (session, file) within LOOM_FILE_CONTEXT_TTL_MIN (default 30);
// the tenth edit of the same file costs nothing. The same
// request reads the repository's primary code namespace (team file rules and
// design decisions, marked "Team rule (by …)" or "Decision (by …)") when the
// checkout's cached repo entry names one (a fresh cache: the primer and the
// shunt write it).
//
//   LOOM_FILE_CONTEXT=off             disable
//   LOOM_FILE_CONTEXT_TOKENS=300      budget for the block's rows other than decisions
//                                     and constraints, which the server prints whole
//   LOOM_FILE_CONTEXT_TTL_MIN=30      re-inject for the same file after this

import {
  gitToplevel,
  postBounded,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
  envSwitch,
} from "../lib/loom-client.mjs";
import {
  contextPayload,
  decideInject,
  lookupFileContext,
  relativeForLookup,
  targetPath,
  teamRulesNamespace,
} from "../lib/file-context.mjs";
import { readCache } from "../lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";

const ENABLED = envSwitch("LOOM_FILE_CONTEXT", true);
const TOKENS = Number(process.env.LOOM_FILE_CONTEXT_TOKENS || 300);
const TTL_MS = Number(process.env.LOOM_FILE_CONTEXT_TTL_MIN || 30) * 60 * 1000;

async function main() {
  if (!ENABLED) {
    scheduleExit(0);
    return;
  }
  const token = resolveToken();
  if (!token) {
    scheduleExit(0);
    return;
  }

  const payload = await readStdinJson();
  const file = targetPath(payload.tool_name, payload.tool_input);
  if (!file) {
    scheduleExit(0);
    return;
  }

  const cwd = payload.cwd || process.cwd();
  const root = gitToplevel(cwd);
  const rel = relativeForLookup(root, file, cwd);

  const decision = decideInject(readSessionState("file-context", payload.session_id), rel, Date.now(), TTL_MS);
  if (!decision.inject) {
    scheduleExit(0);
    return;
  }

  const scope = resolveScope(cwd);
  // The checkout's cached repo entry (the shunt's and the
  // primer's, never a lookup here) names the repository's primary code
  // namespace; its team file rules ride the same one request.
  const entry = root ? readCache(root) : null;
  const rendered = await lookupFileContext({
    scope,
    rel,
    tokenBudget: TOKENS,
    entry,
    post: postBounded,
    bound: { token, serverUrl: resolveServerUrl() },
  });
  // Record the lookup whether or not it hit: an empty answer is also an answer
  // for this session.
  writeSessionState("file-context", payload.session_id, decision.cache);

  if (rendered) {
    const teamNamespace = teamRulesNamespace(entry, scope);
    process.stdout.write(JSON.stringify(contextPayload(rendered, { rel, teamNamespace })) + "\n");
  }
  scheduleExit(0);
}

main().catch(() => scheduleExit(0));
