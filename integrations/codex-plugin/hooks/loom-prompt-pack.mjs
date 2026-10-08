#!/usr/bin/env node
// loom-prompt-pack — UserPromptSubmit. On by default; LOOM_PROMPT_PACK=off turns it off.
//
// For each user prompt in a loom-indexed repo, run one bounded code.search
// (top 3, no bodies, no risk annotations) and inject the pointers the index
// is confident about and the session has not seen. See lib/prompt-pack.mjs
// for why it is pointer-only.
//
//   LOOM_PROMPT_PACK=off            disable
//   LOOM_PROMPT_PACK_MIN_CONF=0.4   skip below this retrieval_confidence
//   LOOM_PROMPT_PACK_TOP_K=3        pointers per prompt
//   LOOM_PROMPT_PACK_TIMEOUT_MS=2500 the search's bound: the prompt waits on it

import {
  envNumber,
  envSwitch,
  gitToplevel,
  postBounded,
  readStdinJson,
  resolveServerUrl,
  resolveToken,
  runHookMain,
  scheduleExit,
} from "../lib/loom-client.mjs";
import { resolveIndexedRepo } from "../lib/repo-scope.mjs";
import { accept, packPayload, renderPack, searchQuery, shouldAsk } from "../lib/prompt-pack.mjs";
import { readJsonFile, sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const ENABLED = envSwitch("LOOM_PROMPT_PACK", true);
// The user's prompt waits on this search; pointers that arrive later help no one.
const TIMEOUT_MS = Math.round(envNumber("LOOM_PROMPT_PACK_TIMEOUT_MS", 2500, { min: 200 }));
const MIN_CONF = envNumber("LOOM_PROMPT_PACK_MIN_CONF", 0.4, { min: 0, max: 1 });
const TOP_K = Math.round(envNumber("LOOM_PROMPT_PACK_TOP_K", 3, { min: 1, max: 5 }));

/** The checkout's indexed repository: the resolver every hook shares
 *  (lib/repo-scope.mjs — its cache, its candidates). */
function indexedRepo(root) {
  return resolveIndexedRepo(root, { consumer: "claude-code-prompt-pack", timeoutMs: TIMEOUT_MS });
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

  const cwd = payload.cwd || process.cwd();
  const root = gitToplevel(cwd);
  if (!root) {
    scheduleExit(0);
    return;
  }
  const repo = await indexedRepo(root);
  if (!repo.indexed) {
    scheduleExit(0);
    return;
  }

  const res = await postBounded(
    "code.search",
    {
      org: repo.org,
      namespace: repo.namespace,
      query: searchQuery(prompt).slice(0, 2000),
      top_k: TOP_K,
      token_budget: 600,
      bodies: "none",
      annotate: false,
      consumer: "claude-code-prompt-pack",
    },
    { token, serverUrl: resolveServerUrl(), timeoutMs: TIMEOUT_MS },
  );
  if (!accept(res, MIN_CONF)) {
    scheduleExit(0);
    return;
  }

  const sp = sessionStatePath("prompt-pack", payload.session_id);
  const { text, seen: next } = renderPack(res, readJsonFile(sp, []), { cap: TOP_K });
  writeJsonFile(sp, next);
  if (text) process.stdout.write(JSON.stringify(packPayload(text)) + "\n");
  scheduleExit(0);
}

runHookMain(main);
