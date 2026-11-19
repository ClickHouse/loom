#!/usr/bin/env node
// loom-prompt-pack — UserPromptSubmit. On by default; LOOM_PROMPT_PACK=off turns it off.
//
// Select one bounded lookup for a qualifying indexed-code question: symbol,
// callers or impact for a clear named target, otherwise search. Self-contained
// and local-only tasks skip it. Render the actual evidence without a cross-turn
// cache; later prompts may follow working-tree or index changes.
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
import { accept, codeLookup, lookupBody, packPayload, renderPack } from "../lib/prompt-pack.mjs";
import { symbolAnswerText } from "../lib/code-shunt.mjs";
import { renderBlast } from "../lib/blast-radius.mjs";
import { recalledDataBlock } from "../lib/prompt-recall.mjs";

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

function requestLookup(op, body, options) {
  if (op === "code.symbol") return postBounded("code.symbol", body, options);
  if (op === "code.callers") return postBounded("code.callers", body, options);
  if (op === "code.impact") return postBounded("code.impact", body, options);
  return postBounded("code.search", body, options);
}

function renderLookup(lookup, response, repo) {
  if (!response || response.abstain || response.ambiguous) return "";
  if (lookup.op === "code.search") return accept(response, MIN_CONF) ? renderPack(response, { cap: TOP_K }).text : "";
  if (lookup.op === "code.symbol") {
    const text = symbolAnswerText(response, { asOf: repo.asOf, namespace: repo.namespace });
    const definition = response.definitions?.[0];
    const signature = definition?.signature;
    const answer = text && signature ? `${text} Signature: ${String(signature).slice(0, 160)}` : text;
    if (!answer || !lookup.includeBody || response.definitions.length !== 1 || definition.elided || !definition.body)
      return answer;
    return `${answer}\n${recalledDataBlock(definition.body)}`;
  }
  if (lookup.op === "code.impact") return renderBlast(lookup.name, response, { maxNames: TOP_K });
  const results = (response.edges || [])
    .filter((e) => e.path && e.line && e.from_symbol)
    .map((e) => ({
      path: e.path,
      start_line: e.line,
      symbol: `${e.from_symbol} --${e.edge_kind || "unspecified"}--> ${e.to_symbol || "target not returned"}`,
    }));
  const precision =
    response.epistemic === "exact"
      ? "exact for returned indexed relationships"
      : `counts are floors (${(response.causes || []).join(", ") || "indexed code only"})`;
  const bounded = response.truncated ? "; response truncated" : "";
  return renderPack(
    { results },
    {
      cap: TOP_K,
      header: `Loom code.callers returned indexed relationships (showing at most ${TOP_K}); ${precision}${bounded}:`,
    },
  ).text;
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
  const lookup = codeLookup(prompt, token);
  if (!lookup) {
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

  const body = lookupBody(repo, lookup, { topK: TOP_K, token });
  if (!body) return;
  const res = await requestLookup(lookup.op, body, {
    token,
    serverUrl: resolveServerUrl(),
    timeoutMs: TIMEOUT_MS,
  });
  const text = renderLookup(lookup, res, repo);
  if (text) process.stdout.write(JSON.stringify(packPayload(text)) + "\n");
  scheduleExit(0);
}

runHookMain(main);
