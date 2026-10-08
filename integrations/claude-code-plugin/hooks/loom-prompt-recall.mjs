#!/usr/bin/env node
// loom-prompt-recall — UserPromptSubmit.
//
// One bounded context.pack(mode=task) restricted to the planes a prompt can
// use mid-session — memories, decisions, procedures — with the prompt as the
// query. Injects the plane-tagged blocks the session has not seen, only when
// memory is not abstaining and its retrieval_confidence clears the bar.
//
//   LOOM_PROMPT_RECALL=first           (default) the first qualifying prompt only
//   LOOM_PROMPT_RECALL=on|off          every qualifying prompt | never
//   LOOM_PROMPT_RECALL_MIN_CONF=0.5    skip below this retrieval_confidence
//   LOOM_PROMPT_RECALL_TOP_K=3         hits per plane
//   LOOM_PROMPT_RECALL_BUDGET=600      context.pack token_budget
//   LOOM_PROMPT_RECALL_MAX_CHARS=1200  injected text cap

import fs from "node:fs";
import {
  envNumber,
  postBounded,
  readStdinJson,
  resolveScope,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { shouldAsk } from "../lib/prompt-pack.mjs";
import { acceptPack, recallBody, recallPayload, renderRecall } from "../lib/prompt-recall.mjs";
import { readJsonFile, sessionStatePath, writeCursor, writeJsonFile } from "../lib/tmp-state.mjs";

// "first" recalls once, when the task is stated, and leaves later lookups to
// the model: one bounded call per session.
const MODE = (process.env.LOOM_PROMPT_RECALL || "first").toLowerCase();
const ENABLED = MODE === "on" || MODE === "first";
const MIN_CONF = envNumber("LOOM_PROMPT_RECALL_MIN_CONF", 0.5, { min: 0, max: 1 });
const TOP_K = Math.round(envNumber("LOOM_PROMPT_RECALL_TOP_K", 3, { min: 1, max: 5 }));
const BUDGET = Math.round(envNumber("LOOM_PROMPT_RECALL_BUDGET", 600, { min: 200, max: 2000 }));
const MAX_CHARS = Math.round(envNumber("LOOM_PROMPT_RECALL_MAX_CHARS", 1200, { min: 200 }));

const firstMarkerPath = (sessionId) => sessionStatePath("prompt-recall-first", sessionId, { ext: "" });

export function firstPromptDone(sessionId) {
  return fs.existsSync(firstMarkerPath(sessionId));
}

export function markFirstPromptDone(sessionId) {
  writeCursor(firstMarkerPath(sessionId), Date.now());
}

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
  const prompt = String(payload.prompt || "");
  if (!shouldAsk(prompt)) {
    scheduleExit(0);
    return;
  }
  if (MODE === "first" && firstPromptDone(payload.session_id)) {
    scheduleExit(0);
    return;
  }

  const scope = resolveScope(payload.cwd || process.cwd());
  const res = await postBounded("context.pack", recallBody(scope, payload, prompt, { budget: BUDGET, topK: TOP_K }), {
    token,
    serverUrl: resolveServerUrl(),
  });
  if (!acceptPack(res, MIN_CONF)) {
    scheduleExit(0);
    return;
  }

  const sp = sessionStatePath("prompt-recall", payload.session_id);
  const { text, seen: nextSeen } = renderRecall(res, readJsonFile(sp, []), { cap: 3, maxChars: MAX_CHARS });
  writeJsonFile(sp, nextSeen);
  if (MODE === "first") {
    // One recall per session: the next prompt finds the marker and exits
    // before any request (see firstPromptDone / markFirstPromptDone).
    markFirstPromptDone(payload.session_id);
  }
  if (text) process.stdout.write(JSON.stringify(recallPayload(text)));
  scheduleExit(0);
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") runHookMain(main);
