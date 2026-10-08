#!/usr/bin/env node
// Loom session memory — SessionEnd hands the session's real turns to
// memory.set_from_messages(defer=true), once. The server extracts the facts,
// writes the session summary, judges the outcome and distils reflections; the
// hook itself runs no model and stores no transcript (see lib/session-memory.mjs).
//
// Off-switch: LOOM_SESSION_MEMORY=off. Needs a writer token; fire-and-forget
// through the detached flusher so the agent never waits on the request.

import { unlinkSync } from "node:fs";

import {
  envSwitch,
  readStdinJson,
  resolveScope,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { dispatchFlush } from "../lib/flush-job.mjs";
import { sessionMemoryBody, sessionToolEchoes, transcriptTurns } from "../lib/session-memory.mjs";
import { readRecalledStash, recalledStashPath } from "../lib/run-hook.mjs";
import { parseLines, readTail } from "../lib/transcript.mjs";
import { journalTurns, readJournal } from "../lib/turn-journal.mjs";

const MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024; // read the tail of a huge transcript only

async function main() {
  const event = process.argv[2] || "";
  if (event !== "SessionEnd") return scheduleExit(0);
  if (!envSwitch("LOOM_SESSION_MEMORY", true)) return scheduleExit(0);
  if (!resolveToken()) return scheduleExit(0);
  const payload = await readStdinJson();
  const transcript = payload.transcript_path || "";
  // The transcript's turns when the client names one this script can read;
  // the turn journal's otherwise.
  const objs = transcript ? parseLines(readTail(transcript, MAX_TRANSCRIPT_BYTES)) : [];
  let turns = objs.length ? transcriptTurns(objs) : [];
  const echoes = sessionToolEchoes(objs);
  if (!turns.length) turns = journalTurns(readJournal(payload.session_id || ""));
  if (!turns.length) return scheduleExit(0);
  const sid = payload.session_id || "";
  // Hand the extractor the keys this session recalled
  // (the capture hook's stash); this is the last SessionEnd hook, so it
  // removes the file.
  const recalledKeys = readRecalledStash(sid);
  const body = sessionMemoryBody(resolveScope(payload.cwd), payload, turns, { recalledKeys, echoes });
  try {
    unlinkSync(recalledStashPath(sid));
  } catch {
    /* fine */
  }
  if (!body) return scheduleExit(0);
  // Spawn failure: nothing is lost that the next session cannot say again.
  dispatchFlush({ posts: [{ op: "memory.set_from_messages", body }] });
  scheduleExit(0);
}

runHookMain(main);
