// Pure helpers for the loom-prompt-pack UserPromptSubmit hook (unit-tested).
//
// On by default (LOOM_PROMPT_PACK=off turns it off). Per-TOOL-TURN injection
// (~1000 tokens every turn) blows a context budget; this is per PROMPT, pointers
// only (no bodies), at most three, gated on the index's own abstain and
// confidence, and never the same pointer twice in a session. It is the cost
// lever: the agent gets the index's answer with the prompt instead of spending
// a turn asking for it, and skips the first exploration round.

import { redactHead } from "./loom-client.mjs";

export const MIN_PROMPT_CHARS = 12;
export const SEEN_CAP = 40;

/** Skip short prompts ("yes", "continue", "/compact") and slash commands. */
export function shouldAsk(prompt, minChars = MIN_PROMPT_CHARS) {
  const p = String(prompt || "").trim();
  if (p.length < minChars) return false;
  if (p.startsWith("/")) return false;
  // A question about a change in history is answered from git history: code pointers
  // only send the agent to read files that do not answer it.
  if (HISTORY_RE.test(searchQuery(p))) return false;
  return true;
}

// "Which change / PR made X …", "when did X start …", "since which release …".
const HISTORY_RE =
  /\b(which|what)\s+(change|commit|pr|pull request|release|version)\b|\bwhen did\b|\bsince (which|what)\b|\b(introduced|regressed) (by|in)\b/i;

// The open tag of a block the request is wrapped in by a template or another tool.
const WRAPPED_OPEN_RE = /<(question|task|issue|request|query|problem)>/gi;
// A sentence that tells the agent how to work or answer, not what to look for.
const INSTRUCTION_RE =
  /\b(do not|don't|never)\s+(edit|modify|change|write|run|build)\b|\b(end|finish|format)\s+your\s+(reply|answer|response)\b|\breply\s+with\b|\bJSON\s+list\b|\bfor example\s+\[/i;

/** The text to search the index with: the request inside a wrapper block when
 *  there is one, without the sentences that only instruct the agent (how to
 *  work, how to format the answer) — their words match CI scripts and tests,
 *  not the code the request is about. */
export function searchQuery(prompt) {
  const p = String(prompt || "");
  const wrapped = wrappedBlock(p);
  const body = wrapped !== null && wrapped.length >= MIN_PROMPT_CHARS ? wrapped : p;
  const kept = body.split(/(?<=[.!?])\s+|\n{2,}/).filter((sentence) => !INSTRUCTION_RE.test(sentence));
  const q = kept.join(" ").replace(/\s+/g, " ").trim();
  return q.length >= MIN_PROMPT_CHARS ? q : p.trim();
}

/** The trimmed text of the first wrapper block that has a close tag, or null.
 *  Each tag name's close is searched for once, so an unclosed tag costs one pass. */
function wrappedBlock(p) {
  const unclosed = new Set();
  for (const open of p.matchAll(WRAPPED_OPEN_RE)) {
    const name = open[1].toLowerCase();
    if (unclosed.has(name)) continue;
    const start = open.index + open[0].length;
    const closeRe = new RegExp(`</${name}>`, "gi");
    closeRe.lastIndex = start;
    const close = closeRe.exec(p);
    if (close) return p.slice(start, close.index).trim();
    unclosed.add(name);
  }
  return null;
}

/** Body for the per-prompt code.search. The query is redacted: code.search
 *  keeps it in loom.code_search_trace. */
export function searchBody(repo, prompt, { topK = 3, token } = {}) {
  return {
    org: repo.org,
    namespace: repo.namespace,
    query: redactHead(searchQuery(prompt), 2000, token).slice(0, 2000),
    top_k: topK,
    token_budget: 600,
    bodies: "none",
    annotate: false,
    consumer: "claude-code-prompt-pack",
  };
}

/** Gate on the index's own verdict, not on the caller's optimism. */
export function accept(res, minConfidence = 0.4) {
  if (!res || res.abstain) return false;
  if (!Array.isArray(res.results) || !res.results.length) return false;
  return Number(res.retrieval_confidence || 0) >= minConfidence;
}

export function pointerOf(unit) {
  const s = Number(unit.start_line || 0);
  const e = Number(unit.end_line || 0);
  return e > s ? `${unit.path}:${s}-${e}` : `${unit.path}:${s}`;
}

/**
 * Render ≤ `cap` pointers the session has not seen, and the updated seen list.
 * Returns {text, seen}. text is "" when every hit was already shown.
 */
export function renderPack(res, seen, { cap = 3 } = {}) {
  const seenSet = new Set(Array.isArray(seen) ? seen : []);
  const lines = [];
  const next = [...seenSet];
  for (const u of res.results || []) {
    const ptr = pointerOf(u);
    if (!u.path || seenSet.has(ptr)) continue;
    seenSet.add(ptr);
    next.push(ptr);
    const sym = u.symbol ? ` ${u.symbol}` : "";
    const sig = u.signature ? ` — ${String(u.signature).slice(0, 80)}` : "";
    lines.push(`- ${ptr}${sym}${sig}`);
    if (lines.length >= cap) break;
  }
  while (next.length > SEEN_CAP) next.shift();
  if (!lines.length) return { text: "", seen: next };
  const conf = Number(res.retrieval_confidence || 0).toFixed(2);
  const text =
    `Loom's code index, asked this prompt (confidence ${conf}), ranks these units first:\n` +
    `${lines.join("\n")}\n` +
    `This is the index's answer for this prompt: do not run the search again. To check one, ` +
    `Read only its line range; call another code tool only for what these leave open.`;
  return { text, seen: next };
}

export function packPayload(text) {
  return {
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: String(text || ""),
    },
  };
}
