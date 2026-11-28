#!/usr/bin/env node
// loom-run — the run ledger hooks.
//
//   UserPromptSubmit  → on the FIRST prompt of a session, run.begin with the
//                       prompt as the goal (run_id = cc-<session_id>, or
//                       the id the server answers instead). Under Claude
//                       Code loom-record's hook keeps the begin in the run
//                       state and its detached process asks it here, so the
//                       prompt does not wait for the answer.
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
  planeEnabled,
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
  keepRunBegin,
  lastAssistantText,
  readRecalledStash,
  readRunState,
  runLedgerEnabled,
  writeRunBegun,
} from "../lib/run-hook.mjs";
import { sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";
import { appendTurn, lastAssistant, readJournal } from "../lib/turn-journal.mjs";

const ENABLED = planeEnabled("ledger") && runLedgerEnabled();

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
    // On disk before it is asked, with the body every retry re-sends (the README's
    // hooks table has the retry rules): a SessionEnd or a second prompt that comes
    // before the answer finds a begin to ask again. loom-record keeps it in the hook
    // and starts this script in a detached process on a job file. That process asks
    // only what it finds kept: a begin sent after a SessionEnd has asked it and closed
    // the run would open the run again.
    const kept = process.argv[4] ? readRunState(sessionId) : keepRunBegin(payload, () => scope);
    if (!kept?.attempted || !kept.body || kept.refused) {
      scheduleExit(0);
      return;
    }
    // Awaited: the server may answer another id than the derived one, and later hooks use it.
    const { body, at } = kept;
    const res = await postBoundedResult("run.begin", body, opts);
    // A SessionEnd or a later prompt that came before the answer has written its own
    // state: marked begun now, a run SessionEnd closed would keep a resumed session's
    // next prompt from opening one.
    if (readRunState(sessionId).at !== at) {
      scheduleExit(0);
      return;
    }
    const runId = res.status >= 200 && res.status < 300 && res.body ? res.body.run_id : "";
    // 408, 429 and a 2xx with no usable id may still have opened the run.
    const transient = res.status === 408 || res.status === 429 || res.status >= 500 || res.status < 300;
    if (typeof runId === "string" && runId) writeRunBegun(sessionId, runId);
    else if (transient) writeJsonFile(sp, { attempted: true, body, at: Date.now() });
    else if (kept.retry) writeJsonFile(sp, { refused: true, attempted: true, body, at: Date.now() });
    else writeJsonFile(sp, { refused: true, at: Date.now() });
    scheduleExit(0);
    return;
  }

  if (event === "SessionEnd") {
    // Read before a request is awaited: loom-session-memory, which runs
    // beside this script, removes the stash.
    const recalled = readRecalledStash(sessionId);
    let state = readRunState(sessionId);
    let completeOpts = opts;
    let deadline;
    if (!state.begun && state.attempted) {
      // Asked with no answer: the begin may have opened a run. Asked again, it answers
      // that run's id (or opens one, which this hook then closes), with the body first sent.
      // Begin and complete share one deadline inside Codex's one-second SessionEnd budget.
      deadline = Date.now() + 900;
      const beginOpts = { ...opts, timeoutMs: Math.min(opts.timeoutMs, 400) };
      const res = await postBoundedResult("run.begin", state.body || beginBody(scope, payload, ""), beginOpts);
      const runId = res.status >= 200 && res.status < 300 && res.body ? res.body.run_id : "";
      if (typeof runId === "string" && runId) writeRunBegun(sessionId, runId);
      // No answer: stamped anew, so one the prompt's own ask gets after this hook is
      // dropped. It would mark begun a run that has no close to come. An answer that
      // ask got during this wait is on disk already and stays: begun, it is closed below.
      else if (readRunState(sessionId).at === state.at) writeJsonFile(sp, { ...state, at: Date.now() });
      state = readRunState(sessionId);
    }
    if (!state.begun) {
      scheduleExit(0);
      return;
    } // no run to close
    const transcript = payload.transcript_path || "";
    const finalText =
      (transcript ? lastAssistantText(parseLines(readTail(transcript))) : "") || lastAssistant(readJournal(sessionId));
    // Awaited, then an exit at once: a host gives its SessionEnd hooks a
    // short shared budget (Codex one second), and a process that lingers
    // past its own request is reported as cancelled even after the row
    // landed. loom-record runs this script beside others in one process,
    // where scheduleExit leaves the exit to it.
    const body = completeBody(scope, payload, finalText, process.env, recalled);
    // No longer begun, so a resumed session's next prompt opens a new run. The
    // id stays for the other SessionEnd hooks, which run beside this one; the
    // stale-file sweep removes it later.
    writeJsonFile(sp, { run_id: state.run_id });
    if (deadline !== undefined) {
      // Body and state preparation spend the same budget as both requests.
      const remaining = deadline - Date.now();
      if (remaining <= 0) return scheduleExit(0, 0);
      completeOpts = { ...opts, timeoutMs: Math.min(opts.timeoutMs, remaining) };
    }
    await postBounded("run.complete", body, completeOpts);
    // The stash is removed by loom-session-memory, the last SessionEnd hook.
    return scheduleExit(0, 0);
  }
  scheduleExit(0);
}

runHookMain(main);
