#!/usr/bin/env node
// loom-file-context — PostToolUse (Read|Grep|Bash), PreToolUse
// (Edit|Write|MultiEdit|NotebookEdit) and PreCompact.
//
// When the agent first reads a file locally, and before it edits one it has
// not read, ask Loom what it knows about THAT file (`memory.for_file`:
// memories whose files[] facet names it) and hand the rendered block to the
// model as additionalContext: on the read's result, or before the edit.
// A constraint that arrives after the edit is a review comment; before the
// edit it is a constraint. The agent does not have to remember to search.
//
// Posture matches the other hooks: fail-open, bounded requests, never a
// permission decision — this hook only ADDS context, and costs no model
// request. The same request reads the repository's primary code namespace
// (team file rules and design decisions, marked "Team rule (by …)" or
// "Decision (by …)") when the checkout's cached repo entry names one (a
// fresh cache: the primer and the shunt write it).
//
// One lookup per session, agent context and file. Seen files are kept in
// $TMPDIR (sessionStatePath "file-context") by their path on disk, keyed
// under the subagent's id for a call a subagent made, and forgotten at
// PreCompact, since a compaction drops the blocks from the context.
//   - After a read (Read, a Grep of one file, a shell read or one-file grep of
//     files in the checkout): a file seen is not asked about again. At most
//     READ_FILES_PER_CALL files per call, asked side by side under one
//     READ_LOOKUP_TIMEOUT_MS bound, and READ_LOOKUPS_PER_SESSION lookups per
//     session; READ_UNANSWERED_PER_SESSION lookups the server did not answer
//     end them for the session. An unanswered file stays unseen.
//   - Before an edit: a file seen within LOOM_FILE_CONTEXT_TTL_MIN (default
//     30) is not asked about again, so the tenth edit costs nothing and a
//     rule written mid-session still arrives; the request has the hooks'
//     bound. A lookup the server did not answer holds off the file's next
//     edit lookup for as long, and leaves the file unseen for a read.
//
//   LOOM_FILE_CONTEXT=off             disable
//   LOOM_FILE_CONTEXT_TOKENS=300      budget for the block's rows other than decisions
//                                     and constraints, which the server prints whole
//   LOOM_FILE_CONTEXT_TTL_MIN=30      before an edit, re-inject for the same file after this

import fs from "node:fs";
import path from "node:path";
import {
  gitToplevel,
  pathUnder,
  planeEnabled,
  postBounded,
  readStdinJson,
  realPath,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  scheduleExit,
  envSwitch,
} from "../lib/loom-client.mjs";
import {
  READ_FILES_PER_CALL,
  READ_LOOKUP_TIMEOUT_MS,
  contextPayload,
  decideInject,
  lookupFileContext,
  readContextPayload,
  readLookupsLeft,
  readPaths,
  relativeForLookup,
  seenKey,
  targetPath,
  teamRulesNamespace,
} from "../lib/file-context.mjs";
import { readCache } from "../lib/repo-scope.mjs";
import { readSessionState, writeSessionState } from "../lib/tmp-state.mjs";

const EVENT = process.argv[2] || "";
const ENABLED = planeEnabled("memory") && envSwitch("LOOM_FILE_CONTEXT", true);
const TOKENS = Number(process.env.LOOM_FILE_CONTEXT_TOKENS || 300);
const TTL_MS = Number(process.env.LOOM_FILE_CONTEXT_TTL_MIN || 30) * 60 * 1000;
// A read's result is held for its lookups, never longer than this: stdin, git
// and the request each have a bound of their own, and this one ends the
// process whatever hangs.
const READ_EXIT_MS = 2 * READ_LOOKUP_TIMEOUT_MS;
const STATE = "file-context";

/** Mark `keys` seen and add to the session's counts, on the state as it is
 *  now: another hook of the session may have written it during the lookup. */
function remember(sessionId, keys, { reads = 0, unanswered = 0 } = {}) {
  let state = readSessionState(STATE, sessionId);
  const now = Date.now();
  for (const key of keys) state = decideInject(state, key, now, 0).cache;
  if (reads) {
    state.reads = Number(state.reads || 0) + reads;
    state.unanswered = Number(state.unanswered || 0) + unanswered;
  }
  writeSessionState(STATE, sessionId, state);
}

function isFile(abs) {
  try {
    return fs.statSync(abs).isFile();
  } catch {
    return false;
  }
}

/** `abs` as the file system names it, a symlink to a file under its own name:
 *  in a checkout it is a path of its own, as pathUnder reads it, and its
 *  target is another file. */
function diskPath(abs) {
  let link = false;
  try {
    link = fs.lstatSync(abs).isSymbolicLink();
  } catch {
    // A path that does not exist yet (a Write's new file) is no link.
  }
  return link ? path.join(realPath(path.dirname(abs)), path.basename(abs)) : realPath(abs);
}

/** After a read: the block of each file of the checkout the call read that
 *  this agent context has not been shown. A call that read only seen files
 *  costs no git call and no request. */
async function afterRead(payload, token) {
  scheduleExit(0, READ_EXIT_MS);
  const cwd = payload.cwd || process.cwd();
  const key = (abs) => seenKey(payload.agent_id, abs);
  const state = readSessionState(STATE, payload.session_id);
  const now = Date.now();
  // A directory or a missing path has no memory to look up.
  const fresh = [...new Set(readPaths(payload.tool_name, payload.tool_input, cwd).filter(isFile).map(diskPath))].filter(
    (abs) => decideInject(state, key(abs), now, Infinity).inject,
  );
  const left = Math.min(READ_FILES_PER_CALL, readLookupsLeft(state));
  const root = fresh.length && left ? gitToplevel(cwd) : "";
  if (!root) return;
  // Nor has a file outside the checkout.
  const unseen = fresh
    .map((abs) => ({ abs, rel: pathUnder(root, abs) }))
    .filter((f) => f.rel)
    .slice(0, left);
  if (!unseen.length) return;

  const scope = resolveScope(cwd);
  const entry = readCache(root);
  const bound = { token, serverUrl: resolveServerUrl(), timeoutMs: READ_LOOKUP_TIMEOUT_MS };
  const answers = await Promise.all(
    unseen.map(({ rel }) => lookupFileContext({ scope, rel, tokenBudget: TOKENS, entry, post: postBounded, bound })),
  );
  // An empty answer is also an answer for this session. An unanswered file
  // stays unseen: its next read, or its edit, asks again.
  const answered = unseen.filter((_, i) => answers[i] !== null);
  remember(
    payload.session_id,
    answered.map((f) => key(f.abs)),
    { reads: unseen.length, unanswered: unseen.length - answered.length },
  );
  const teamNamespace = teamRulesNamespace(entry, scope);
  const found = unseen.map(({ rel }, i) => ({ rel, rendered: answers[i], teamNamespace })).filter((f) => f.rendered);
  if (found.length) process.stdout.write(JSON.stringify(readContextPayload(found)) + "\n");
}

/** Before an edit: the target file's block, unless this agent context was
 *  shown it within the TTL. */
async function beforeEdit(payload, token) {
  const file = targetPath(payload.tool_name, payload.tool_input);
  if (!file) return;
  const cwd = payload.cwd || process.cwd();
  const key = seenKey(payload.agent_id, diskPath(path.resolve(cwd, file)));
  // An edit lookup the server did not answer is kept under its own key: it
  // holds off the file's next edit lookup, and the file stays unseen for a read.
  const unanswered = `unanswered-edit\n${key}`;
  const state = readSessionState(STATE, payload.session_id);
  const now = Date.now();
  if (!decideInject(state, key, now, TTL_MS).inject || !decideInject(state, unanswered, now, TTL_MS).inject) return;

  const root = gitToplevel(cwd);
  const rel = relativeForLookup(root, file, cwd);
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
  // An empty answer is also an answer for this session.
  remember(payload.session_id, [rendered === null ? unanswered : key]);

  if (rendered) {
    const teamNamespace = teamRulesNamespace(entry, scope);
    process.stdout.write(JSON.stringify(contextPayload(rendered, { rel, teamNamespace })) + "\n");
  }
}

async function main() {
  if (!ENABLED) return;
  const token = resolveToken();
  if (!token) return;
  const payload = await readStdinJson();
  if (EVENT === "PreCompact") {
    // The blocks go with the compacted context: every file is unseen again.
    // The session's lookup counts stay.
    const state = readSessionState(STATE, payload.session_id);
    if (Object.keys(state.seen || {}).length) writeSessionState(STATE, payload.session_id, { ...state, seen: {} });
  } else if (EVENT === "PostToolUse") await afterRead(payload, token);
  else await beforeEdit(payload, token);
}

main()
  .catch(() => {})
  .finally(() => scheduleExit(0));
