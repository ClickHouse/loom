#!/usr/bin/env node
// loom-run — the run ledger hooks.
//
//   UserPromptSubmit  → on the FIRST prompt of a session, run.begin with the
//                       prompt as the goal (run_id = cc-<session_id>, or
//                       the id the server answers instead).
//   SessionEnd        → run.complete with the transcript's last assistant
//                       text as final_response. outcome:"none" — the hook
//                       knows the run ended, not whether it succeeded, and
//                       it never guesses which memories were used.
//
// Every observe.record the capture hook posts carries the same run_id, so
// run.get shows the session's observations, writes and cost without the
// agent doing anything. What only the agent knows — memories_used, a
// reflection — arrives through the run.complete MCP tool when it calls it;
// the server treats a second complete on the same run as a replace.
//
// Bounded; a viewer-only token gets a 403 and nothing is
// recorded. LOOM_RUN_LEDGER=off disables.

import {
  envSwitch,
  postBounded,
  postBoundedResult,
  readStdinJson,
  requestTimeoutMs,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { parseLines, readTail } from "../lib/transcript.mjs";
import {
  beginBody,
  completeBody,
  lastAssistantText,
  readRecalledStash,
  readRunState,
  writeRunBegun,
} from "../lib/run-hook.mjs";
import { sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";
import { appendTurn, lastAssistant, readJournal } from "../lib/turn-journal.mjs";

const ENABLED = envSwitch("LOOM_RUN_LEDGER", true);

async function main() {
  const event = process.argv[2] || "";
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
  const sessionId = payload.session_id || "";
  if (!sessionId) {
    scheduleExit(0);
    return;
  }
  const scope = resolveScope(payload.cwd || process.cwd());
  const opts = { token, serverUrl: resolveServerUrl(), timeoutMs: Math.min(1500, requestTimeoutMs()) };
  const sp = sessionStatePath("run", sessionId);

  if (event === "UserPromptSubmit") {
    const prompt = String(payload.prompt || "");
    // Every prompt goes to the turn journal: the consumers
    // read it when the client hands them no parseable transcript.
    appendTurn(sessionId, "user", prompt);
    const prior = readRunState(sessionId);
    if (prior.begun || prior.refused) {
      scheduleExit(0);
      return;
    }
    // Awaited: the server may answer another run id than the derived one (no
    // Keeper, narrowed token), and every later hook must use that one. Begun only
    // with an answered id. A refusal (4xx) is final for the session. A timeout, a
    // network error or a 5xx is asked again on the next prompt with the same
    // derived id; SessionEnd closes that id if no answer ever came. (Without a
    // Keeper a narrowed token's retry gets a fresh server id, and a run the lost
    // answer opened stays open.)
    const res = await postBoundedResult("run.begin", beginBody(scope, payload, prompt), opts);
    const runId = res.status >= 200 && res.status < 300 && res.body ? res.body.run_id : "";
    if (typeof runId === "string" && runId) writeRunBegun(sessionId, runId);
    else if (res.status >= 400 && res.status < 500) writeJsonFile(sp, { refused: true, at: Date.now() });
    else writeJsonFile(sp, { attempted: true, at: Date.now() });
    scheduleExit(0);
    return;
  }

  if (event === "SessionEnd") {
    const state = readRunState(sessionId);
    // Begun, or asked with no answer (it may have opened under the derived id).
    if (!state.begun && !state.attempted) {
      scheduleExit(0);
      return;
    } // no run to close
    const transcript = payload.transcript_path || "";
    const finalText =
      (transcript ? lastAssistantText(parseLines(readTail(transcript))) : "") || lastAssistant(readJournal(sessionId));
    // Awaited, then a hard exit: SessionEnd hooks share Claude Code's
    // 1.5 s budget by default (hooks.json raises it with `timeout`), and a
    // process that lingers past its own request is reported as "Hook
    // cancelled" even after the row landed.
    const recalled = readRecalledStash(sessionId);
    const body = completeBody(scope, payload, finalText, process.env, recalled);
    // No longer begun, so a resumed session's next prompt opens a new run. The
    // id stays for the other SessionEnd hooks, which run beside this one; the
    // stale-file sweep removes it later.
    writeJsonFile(sp, { run_id: state.run_id });
    await postBounded("run.complete", body, opts);
    // The stash is removed by loom-session-memory, the last SessionEnd hook.
    process.exit(0);
  }
  scheduleExit(0);
}

runHookMain(main);
