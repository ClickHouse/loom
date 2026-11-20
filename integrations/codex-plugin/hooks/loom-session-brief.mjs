#!/usr/bin/env node
// Loom session brief hook — the session's resumable state.
//
//   PreCompact  → save the window the compaction is about to drop
//   SessionEnd  → save whatever the session did since the last save
//
// Reads the transcript from this session's cursor on, turns it into typed
// events (lib/session-brief.mjs — zero model tokens), and posts them to
// `session.brief_save`, which revises the brief `auto/<project>/<branch>`
// server-side. The next session on the branch sees a one-line pointer in its
// SessionStart primer and loads the brief on request (the loom-session skill).
//
// The post goes through the detached flusher: a revision is one model call
// per window (seconds), longer than any hook may block. The cursor moves
// optimistically before the post, so a SessionEnd that fires while the
// PreCompact save is still in flight sends only the newer events; the
// flusher puts it back if the save fails, so the next save retries them.
//
// LOOM_SESSION_BRIEF=off disables. LOOM_SESSION_BRIEF_KEY names the brief.

import os from "node:os";

import {
  envNumber,
  git,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { dispatchFlush } from "../lib/flush-job.mjs";
import { readRecording } from "../lib/recording.mjs";
import { readCursor, sessionStatePath, writeCursor } from "../lib/tmp-state.mjs";
import { completeLines, fileSize, parseLines, readLinesFrom, readRange } from "../lib/transcript.mjs";
import { readJournal } from "../lib/turn-journal.mjs";
import { briefBody, briefEnabled, journalEvents, transcriptEvents, worthSaving } from "../lib/session-brief.mjs";

const TRIGGERS = { PreCompact: "precompact", SessionEnd: "session_end", Stop: "stop" };
// A revision is up to four model calls server-side; the flusher waits for it.
const FLUSH_TIMEOUT_MS = envNumber("LOOM_SESSION_BRIEF_TIMEOUT_MS", 90000, { min: 1000 });
// Transcript bytes one save reads at most: the start of its window and the end.
const WINDOW_HEAD = 2 * 1024 * 1024;
const WINDOW_TAIL = 6 * 1024 * 1024;

export function cursorPath(sessionId, dir = os.tmpdir()) {
  return sessionStatePath("brief", sessionId, { ext: ".offset", tmp: dir });
}

/** Events since the cursor: from the transcript when there is one this
 *  script can read, else from the turn journal. {events, next}. A transcript
 *  cursor is a byte offset; a window larger than WINDOW_HEAD + WINDOW_TAIL
 *  keeps its head and its tail. */
function windowSince(transcript, sessionId, from) {
  const size = transcript ? fileSize(transcript) : 0;
  if (size) {
    const start = from <= size ? from : 0;
    if (size - start <= WINDOW_HEAD + WINDOW_TAIL) {
      const { lines, next } = readLinesFrom(transcript, start, size - start);
      return { events: transcriptEvents(parseLines(lines.map((l) => l.text).join("\n"))), next };
    }
    const head = readLinesFrom(transcript, start, WINDOW_HEAD).lines;
    const tail = completeLines(readRange(transcript, size - WINDOW_TAIL, WINDOW_TAIL), size - WINDOW_TAIL, {
      partialHead: true,
    });
    const objs = parseLines([...head, ...tail].map((l) => l.text).join("\n"));
    return { events: transcriptEvents(objs), next: tail.length ? tail[tail.length - 1].end : start };
  }
  const rows = readJournal(sessionId);
  return { events: journalEvents(rows.slice(from)), next: rows.length };
}

function dispatch(post, cursorFile, advanceTo, rollbackTo) {
  writeCursor(cursorFile, advanceTo);
  if (
    !dispatchFlush({ posts: [post], cursorPath: cursorFile, advanceTo, rollbackTo }, { timeoutMs: FLUSH_TIMEOUT_MS })
  ) {
    writeCursor(cursorFile, rollbackTo);
  }
}

async function main() {
  const event = (process.argv[2] || "").trim() || "SessionEnd";
  if (!briefEnabled()) return scheduleExit(0);
  if (!resolveToken()) return scheduleExit(0);
  const payload = await readStdinJson();
  const sessionId = String(payload.session_id || "");
  if (!sessionId) return scheduleExit(0);
  const transcript = String(payload.transcript_path || "");
  const cwd = String(payload.cwd || process.cwd());

  const cp = cursorPath(sessionId);
  const from = readCursor(cp);
  const { events, next } = windowSince(transcript, sessionId, from);
  if (!events.length || next <= from) return scheduleExit(0);
  // Below the bar the cursor stays put: the window keeps growing until it is
  // worth a brief (or the session ends without one).
  if (!worthSaving(events, { firstSave: from === 0 })) return scheduleExit(0);

  const scope = resolveScope(cwd);
  // An automatic brief follows the owner's Record conversations switch. With
  // recording off (the server said so on a recent call) the window is not sent,
  // and the cursor moves past it, so turning recording back on never sends what
  // happened while it was off. The server refuses the save too; this only spares
  // the round trip.
  if (readRecording(resolveServerUrl(), scope.org, scope.namespace) === false) {
    writeCursor(cp, next);
    return scheduleExit(0);
  }
  const body = briefBody(scope, {
    sessionId,
    branch: git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
    head: git(["rev-parse", "HEAD"], cwd),
    trigger: TRIGGERS[event] || "manual",
    events,
    cursor: next,
  });
  dispatch({ op: "session.brief_save", body }, cp, next, from);
  scheduleExit(0);
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") runHookMain(main);
