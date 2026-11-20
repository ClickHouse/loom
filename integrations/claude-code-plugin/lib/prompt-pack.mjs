// Pure helpers for the loom-prompt-pack UserPromptSubmit hook (unit-tested).
//
// One bounded lookup per qualifying prompt: typed names use the corresponding
// code operation; concepts use search. Render pointers, respect abstention and
// confidence, and deduplicate only within this response so later edits stay visible.

import { redactHead, redactSecrets } from "./loom-client.mjs";

export const MIN_PROMPT_CHARS = 12;

/** Skip short prompts ("yes", "continue", "/compact") and slash commands. */
export function shouldAsk(prompt, minChars = MIN_PROMPT_CHARS) {
  const p = String(prompt || "").trim();
  if (p.length < minChars) return false;
  if (p.startsWith("/")) return false;
  const query = searchQuery(p);
  if (
    SELF_CONTAINED_RE.test(query) ||
    LITERAL_QUERY_RE.test(query) ||
    LOCAL_QUERY_RE.test(query) ||
    CHAT_RE.test(query)
  )
    return false;
  // A question about a change in history is answered from git history: code pointers
  // only send the agent to read files that do not answer it.
  if (HISTORY_RE.test(searchQuery(p))) return false;
  return true;
}

const SELF_CONTAINED_RE =
  /^use only (?:the )?(?:(?:provided|supplied) )?(?:source|context|excerpt|evidence)\b|^return only this text\b/i;
const LITERAL_QUERY_RE =
  /^(?:find|locate|search for|grep for) (?:the )?(?:exact|literal) (?:error )?(?:text|string)\b/i;
const LOCAL_QUERY_RE =
  /^i (?:just )?(?:created|edited) (?:an? |the )?(?:untracked|uncommitted|unindexed)\b|^(?:review|inspect|read|use) (?:my|this|the) (?:uncommitted|untracked|unindexed|working[- ]tree)\b/i;
const CHAT_RE = /^(?:thanks(?:,? that(?:'s| is) all)?|thank you|hello|hi|good morning)[.!\s]*$/i;

/** A local code task can still need saved context; supplied-only tasks cannot. */
export function shouldRecall(prompt, asksMemory) {
  if (!asksMemory) return shouldAsk(prompt);
  const p = String(prompt || "").trim();
  const query = searchQuery(p);
  return p.length >= MIN_PROMPT_CHARS && !p.startsWith("/") && !SELF_CONTAINED_RE.test(query) && !CHAT_RE.test(query);
}

const MEMORY_QUERY_RES = [
  /\b(?:did|have) (?:we|i) (?:decide|choose|say|save)\b|\bcite (?:the )?saved (?:decision|memory|preference)\b|^(?:remember|recall (?:our|my)|save (?:this|that) (?:fact|decision))\b/i,
  /^(?:recall|retrieve|find|show(?: me)?) (?:(?:the|my|our) )?(?:saved|recorded) (?:decisions?|memor(?:y|ies)|preferences?|conventions?|lessons?)\b/i,
  /^what (?:is|are) (?:my|our) (?:(?:saved|recorded) )?(?:decisions?|memor(?:y|ies)|preferences?|conventions?|lessons?)\b/i,
];

export function memoryRequested(prompt) {
  const query = searchQuery(prompt);
  return MEMORY_QUERY_RES.some((pattern) => pattern.test(query));
}
const NAME = "`?([A-Za-z_]\\w*(?:(?:::|\\.)[A-Za-z_]\\w*)*)`?";
const END_NAME = "(?=\\s|[?,;]|:(?!:)|\\.(?:\\s|$)|$)";
const DEFINITION_RES = [
  new RegExp(`^where is ${NAME} (?:defined|implemented)\\b`, "i"),
  new RegExp(`^(?:show|explain)(?: me)? (?:the )?(?:definition|implementation) of ${NAME}${END_NAME}`, "i"),
];
const CALLERS_RES = [
  new RegExp(`^(?:who|what) (?:directly )?calls ${NAME}${END_NAME}`, "i"),
  new RegExp(`^(?:(?:which|what) callers|(?:show|list)(?: me)? (?:the )?callers) of ${NAME}${END_NAME}`, "i"),
];
const IMPACT_RES = [
  new RegExp(`^what would changing ${NAME} affect\\b`, "i"),
  new RegExp(
    `^(?:what|which) (?:code|tests|files) (?:could|would|will|might) be affected if I change(?: how)? ${NAME}${END_NAME}`,
    "i",
  ),
];

/** Select only explicit named lookups; unclear code questions retain semantic search. */
export function codeLookup(prompt, token) {
  if (!shouldAsk(prompt)) return null;
  const query = searchQuery(prompt).slice(0, 2000);
  if (/^what would (?:this change|these changes|it) (?:affect|impact)\b/i.test(query)) return null;
  for (const [op, patterns] of [
    ["code.symbol", DEFINITION_RES],
    ["code.callers", CALLERS_RES],
    ["code.impact", IMPACT_RES],
  ]) {
    for (const pattern of patterns) {
      const match = pattern.exec(query);
      if (!match) continue;
      const name = match[1];
      if (name.length > 256 || redactSecrets(name, token) !== name) return null;
      if (/^(?:a|an|the|this|that|these|those|it|my|our|your)$/i.test(name)) continue;
      if (op === "code.symbol") {
        const includeBody =
          pattern === DEFINITION_RES[1] || /\b(?:what does|how does|explain)\b/i.test(query.slice(match[0].length));
        return { op, name, includeBody };
      }
      return { op, name };
    }
  }
  if (memoryRequested(query)) return null;
  return { op: "code.search", prompt };
}

/** Typed requests carry only a validated identifier, identity and fixed bounds. */
export function lookupBody(repo, lookup, { topK = 3, token } = {}) {
  if (lookup.op === "code.search") return searchBody(repo, lookup.prompt, { topK, token });
  const name = String(lookup.name || "");
  if (
    !/^[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*$/.test(name) ||
    name.length > 256 ||
    redactSecrets(name, token) !== name
  )
    return null;
  const body = {
    org: repo.org,
    namespace: repo.namespace,
    name,
    include_lessons: false,
    consumer: "claude-code-prompt-pack",
  };
  if (lookup.op === "code.symbol")
    return {
      ...body,
      token_budget: 600,
      include_body: lookup.includeBody === true,
      include_history: false,
      include_examples: false,
      uses: false,
      uses_brief: false,
    };
  if (lookup.op === "code.callers") return { ...body, depth: 1, max_nodes: topK, include_ambiguous: false };
  if (lookup.op === "code.impact") return { ...body, depth: 1, per_hop_limit: topK };
  return null;
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
    // the pointers are all this hook renders; the pre-edit hook serves the files' memories
    include_lessons: false,
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
 * Render at most `cap` distinct pointers from this response.
 * Later prompts may need fresh evidence at the same indexed span.
 */
export function renderPack(res, { cap = 3, header } = {}) {
  const seenSet = new Set();
  const lines = [];
  for (const u of res.results || []) {
    const ptr = pointerOf(u);
    if (!u.path || seenSet.has(ptr)) continue;
    seenSet.add(ptr);
    const sym = u.symbol ? ` ${u.symbol}` : "";
    const sig = u.signature ? ` — ${String(u.signature).slice(0, 80)}` : "";
    lines.push(`- ${ptr}${sym}${sig}`);
    if (lines.length >= cap) break;
  }
  if (!lines.length) return { text: "" };
  const conf = Number(res.retrieval_confidence || 0).toFixed(2);
  const text =
    (header || `Loom's code index, asked this prompt (confidence ${conf}), ranks these units first:`) +
    "\n" +
    `${lines.join("\n")}\n` +
    `This is the index's answer for this prompt: do not run the search again. To check one, ` +
    `Read only its line range; call another code tool only for what these leave open.`;
  return { text };
}

export function packPayload(text) {
  return {
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: String(text || ""),
    },
  };
}
