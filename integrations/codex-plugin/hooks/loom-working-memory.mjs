#!/usr/bin/env node
// Loom working-memory hook — the session's "where was I" note.
//
//   Stop        → write the note after each assistant turn (deduped)
//   PreCompact  → write it right before the context window is dropped
//
// One `working_memory.set` per project (block `session:<project>`, agent
// scope): the goal (first prompt), the working tree (`git status --short
// --branch`), the files edited this session (from the transcript), and the
// last assistant text. `context.pack` loads working memory FIRST in the
// SessionStart primer, and Claude Code fires SessionStart again after a
// compaction (`source: "compact"`) and on resume, so the note the hook wrote
// a moment before the drop is the first thing the agent reads after it.
//
// Fire-and-forget with the plugin's hard bounds (postFireAndForget +
// scheduleExit): a hung server costs nothing; a missing token does nothing.
// LOOM_WORKING_MEMORY=off disables. Zero LLM tokens on this path.

import crypto from "node:crypto";

import {
  envSwitch,
  git,
  postFireAndForget,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { lastAssistantText, runIdFor } from "../lib/run-hook.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";
import { TAIL_BYTES, parseLines, readHeadTail } from "../lib/transcript.mjs";
import { firstUser, lastAssistant, readJournal } from "../lib/turn-journal.mjs";
import {
  buildSessionState,
  editedFiles,
  firstUserPrompt,
  parseGitStatus,
  scratchpadAppendBody,
  setBody,
  stateFingerprint,
  turnConclusion,
} from "../lib/working-memory.mjs";

const ENABLED = envSwitch("LOOM_WORKING_MEMORY", true);
// Run scratchpad: one working_memory.append per turn with
// the turn's conclusion, at run scope. Off by default — a write per turn.
const SCRATCHPAD = envSwitch("LOOM_PLUGIN_SCRATCHPAD", false);
// The first prompt lives at the top of the transcript, the edits and the last
// turn at the bottom; the middle of a long session is not needed for the note.
const HEAD_BYTES = 256 * 1024;

function gitStatus(cwd) {
  return parseGitStatus(git(["status", "--short", "--branch"], cwd, { raw: true }));
}

async function main() {
  const event = (process.argv[2] || "").trim() || "Stop";
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
  const sessionId = String(payload.session_id || "");
  const transcript = String(payload.transcript_path || "");
  if (!sessionId) {
    scheduleExit(0);
    return;
  }

  const cwd = String(payload.cwd || process.cwd());
  const scope = resolveScope(cwd);
  // The transcript when the client names one this script can read; the
  // hook payload and the turn journal otherwise.
  const objs = transcript ? parseLines(readHeadTail(transcript, HEAD_BYTES, TAIL_BYTES)) : [];
  const journal = readJournal(sessionId);
  const lastText =
    lastAssistantText(objs) || String(payload.last_assistant_message || "").trim() || lastAssistant(journal);
  const content = buildSessionState({
    goal: firstUserPrompt(objs) || firstUser(journal),
    files: editedFiles(objs),
    git: gitStatus(cwd),
    lastText,
    cwd,
    reason: event,
  });

  // Stop fires after every turn; only write when the note changed.
  const fp = crypto.createHash("sha1").update(stateFingerprint(content)).digest("hex");
  const sp = sessionStatePath("wm", sessionId);
  const state = readJsonFile(sp);
  const prev = state.fp || "";
  const post = { token, serverUrl: resolveServerUrl(), timeoutMs: 1500 };

  // Run scratchpad: the turn's conclusion, once per distinct last text.
  let lastFp = state.lastFp || "";
  if (SCRATCHPAD && event === "Stop") {
    const line = turnConclusion(lastText);
    const lfp = line ? crypto.createHash("sha1").update(line).digest("hex") : "";
    if (line && lfp !== lastFp) {
      postFireAndForget("working_memory.append", scratchpadAppendBody(scope, runIdFor(sessionId), line), post);
      lastFp = lfp;
    }
  }

  if (prev === fp && event !== "PreCompact") {
    if (lastFp !== (state.lastFp || "")) writeJsonFile(sp, { ...state, lastFp });
    scheduleExit(0);
    return;
  }

  postFireAndForget("working_memory.set", setBody(scope, scope.project, content), post);
  writeJsonFile(sp, { fp, lastFp, at: Date.now() });
  scheduleExit(0);
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") runHookMain(main);
