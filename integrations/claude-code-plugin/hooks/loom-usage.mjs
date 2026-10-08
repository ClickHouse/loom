#!/usr/bin/env node
// loom-usage — Stop hook: post the session's own model usage to loom.
//
// Reads the last 1 MiB of the transcript, extracts the assistant turns not
// yet posted (deduped by message id, sidechains skipped), and sends them to
// cost.record_turns, where the server prices them into loom.agent_turn_usage.
// That turns "did loom make this session cheaper" from a hand-run paired
// experiment into a standing series next to loom's own upstream spend.
// Fire-and-forget, bounded; a viewer-only token gets a 403 and nothing is
// recorded. LOOM_USAGE=off disables.

import {
  envSwitch,
  postFireAndForget,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { parseLines, readTail } from "../lib/transcript.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";
import { extractTurns, recordBody } from "../lib/usage.mjs";

const ENABLED = envSwitch("LOOM_USAGE", true);

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
  const transcript = payload.transcript_path || "";
  if (!transcript) {
    scheduleExit(0);
    return;
  }

  const sp = sessionStatePath("usage", payload.session_id);
  const stored = readJsonFile(sp, []);
  const seen = new Set(Array.isArray(stored) ? stored : []);

  const { turns, next } = extractTurns(parseLines(readTail(transcript)), seen);
  writeJsonFile(sp, [...next]);
  if (!turns.length) {
    scheduleExit(0);
    return;
  }

  const scope = resolveScope(payload.cwd || process.cwd());
  postFireAndForget("cost.record_turns", recordBody(scope, payload.session_id, turns), {
    token,
    serverUrl: resolveServerUrl(),
    timeoutMs: 1500,
  });
  scheduleExit(0);
}

runHookMain(main);
