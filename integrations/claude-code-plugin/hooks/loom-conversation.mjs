#!/usr/bin/env node
// Conversation capture: the ONE path that records a session's turns.
//
// On Stop (and SessionEnd as a backstop) it reads the transcript from this
// session's byte cursor, turns user prompts and assistant text into
// conversation.append bodies (msg_id = the transcript uuid, so a resend is
// idempotent server-side) and hands them to the detached flusher, which
// moves the cursor once every turn landed. A client without a parseable
// transcript is served from the turn journal instead. The model never logs
// turns itself: the primer tells it not to.
//
// Off-switch: LOOM_CAPTURE_CONVERSATIONS=false.

import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  contentText,
  envNumber,
  envSwitch,
  postFireAndForget,
  readStdinJson,
  redactSecrets,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
  toolImageTurn,
  truncate,
} from "../lib/loom-client.mjs";
import { dispatchFlush } from "../lib/flush-job.mjs";
import { readRecording } from "../lib/recording.mjs";
import { readCursor, writeCursor } from "../lib/tmp-state.mjs";
import { fileSize, readLinesFrom } from "../lib/transcript.mjs";
import { appendTurn, journalCursorPath, readJournal, readJournalCursor, rowsSince } from "../lib/turn-journal.mjs";

const MAX_CONTENT = 8000; // per-message cap — bounded payloads
const MAX_TURNS_PER_RUN = 50; // safety cap per Stop
const WINDOW_BYTES = 8 * 1024 * 1024; // transcript bytes read per Stop
// Stop fires the instant the agent finishes; the final assistant message can
// still be flushing to the transcript. A short settle lets it land so we
// capture the reply at its own Stop, not one event later. SessionEnd re-reads
// from the cursor as a backstop either way. Tunable / disable with 0.
const SETTLE_MS = envNumber("LOOM_CONV_SETTLE_MS", 350, { min: 0, max: 5000 });

// The cursor is a byte offset into the transcript: how far this session's
// turns have been confirmed by the server.
function cursorPath(sessionId) {
  const safe = sessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
  return join(tmpdir(), `loom-conv-${safe}.offset`);
}

/** Extract a loggable {role, content} from one transcript entry, or null.
 *  Real user prompts have a STRING content, or an array when the user pasted
 *  an image beside the text (the image becomes a "[image: …]" placeholder).
 *  tool_result user turns are skipped unless a tool returned an image, which
 *  leaves one `tool` placeholder turn. Assistant text is the joined `text`
 *  blocks (tool_use/thinking blocks are skipped — we log what was said, not
 *  the scaffolding). */
function turnFrom(obj) {
  const msg = obj && obj.message;
  if (!msg) return null;
  if (obj.type === "user") {
    if (Array.isArray(msg.content) && msg.content.some((b) => b && b.type === "tool_result")) {
      return toolImageTurn(msg.content);
    }
    const content = contentText(msg.content).trim();
    // Skip empties and system-injected/meta turns (<command…>, <system…>).
    if (!content || content.startsWith("<")) return null;
    return { role: "user", content };
  }
  if (obj.type === "assistant" && Array.isArray(msg.content)) {
    const text = contentText(msg.content).trim();
    if (!text) return null; // tool-only assistant step → nothing said
    return { role: "assistant", content: text };
  }
  return null;
}

/** Hand appends to the DETACHED flusher (see the transcript path below for
 *  why): it outlives this hook, awaits every write, and advances the cursor
 *  only when all land. Falls back to inline fire-and-forget without
 *  advancing the cursor, so the next Stop retries. */
function dispatch(appends, cursorFile, advanceTo) {
  // The owner switched recording off for this namespace (the
  // server said so on a recent append): nothing would be kept, so send
  // nothing and move the cursor past these turns. Re-asked after the cache's
  // TTL, so switching it back on is picked up.
  const first = appends[0] || {};
  if (readRecording(resolveServerUrl(), first.org, first.namespace) === false) {
    writeCursor(cursorFile, advanceTo);
    return;
  }
  if (!dispatchFlush({ appends, cursorPath: cursorFile, advanceTo })) {
    for (const a of appends) postFireAndForget("conversation.append", a);
  }
}

/** The event-fed path: a client that hands the hook no
 *  parseable transcript still says, on Stop, what the assistant just
 *  answered (`last_assistant_message`); the prompts were journaled by
 *  loom-run at UserPromptSubmit. Both are posted from the journal's cursor
 *  on, so a Stop carries the prompt(s) since the last Stop plus the reply.
 *  Returns true when it handled the event. */
function fromEvents(payload, sessionId, scope) {
  const reply = typeof payload.last_assistant_message === "string" ? payload.last_assistant_message.trim() : "";
  if (!reply) return false;
  appendTurn(sessionId, "assistant", reply);
  const rows = readJournal(sessionId);
  const cursorFile = journalCursorPath(sessionId);
  const fresh = rowsSince(rows, readJournalCursor(sessionId)).slice(0, MAX_TURNS_PER_RUN);
  if (!fresh.length) return true;
  const token = resolveToken();
  const appends = fresh.map((r) => ({
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    thread_id: sessionId,
    msg_id: `${sessionId}-j${r.i}`,
    role: r.role,
    // Redact before the cut, so a clipped secret cannot slip past its pattern.
    content: truncate(redactSecrets(r.content, token), MAX_CONTENT),
  }));
  dispatch(appends, cursorFile, fresh[fresh.length - 1].i + 1);
  return true;
}

async function main() {
  if (!envSwitch("LOOM_CAPTURE_CONVERSATIONS", true)) {
    return scheduleExit(0);
  }
  const payload = await readStdinJson();
  const sessionId = String(payload.session_id || "");
  const transcript = String(payload.transcript_path || "");
  if (!sessionId) return scheduleExit(0);
  const scope = resolveScope(payload.cwd);
  if (!transcript) {
    fromEvents(payload, sessionId, scope);
    return scheduleExit(0);
  }

  // Let a just-finished assistant message flush before we read (see SETTLE_MS).
  if (SETTLE_MS > 0) await new Promise((r) => setTimeout(r, SETTLE_MS));

  if (!fileSize(transcript)) {
    fromEvents(payload, sessionId, scope); // transcript named but unreadable
    return scheduleExit(0);
  }

  const cp = cursorPath(sessionId);
  const { lines, next } = readLinesFrom(transcript, readCursor(cp), WINDOW_BYTES);

  // The cursor moves past the last line this Stop consumed: a window with
  // more turns than MAX_TURNS_PER_RUN leaves the rest for the next Stop.
  const appends = [];
  let advanceTo = next;
  const token = resolveToken();
  for (const line of lines) {
    if (appends.length >= MAX_TURNS_PER_RUN) {
      advanceTo = line.start;
      break;
    }
    let obj;
    try {
      obj = JSON.parse(line.text);
    } catch {
      continue;
    }
    const turn = turnFrom(obj);
    if (!turn) continue;
    appends.push({
      org: scope.org,
      namespace: scope.namespace,
      agent: scope.agent,
      thread_id: sessionId,
      msg_id: obj.uuid || `${sessionId}-o${line.start}`,
      role: turn.role,
      content: truncate(redactSecrets(turn.content, token), MAX_CONTENT),
    });
  }

  if (appends.length === 0) {
    // Nothing new — advance past scanned lines and exit. A transcript in a
    // shape this script does not parse yields nothing every time; the
    // event-fed path then carries the turns (a Claude Code payload has no
    // last_assistant_message, so it is a no-op there).
    writeCursor(cp, advanceTo);
    fromEvents(payload, sessionId, scope);
    return scheduleExit(0);
  }

  // A conversation.append embeds server-side and can take seconds, so the
  // detached flusher awaits the appends and owns the cursor: it advances only
  // when every turn landed, and the next Stop retries otherwise.
  dispatch(appends, cp, advanceTo);
  scheduleExit(0);
}

runHookMain(main);
