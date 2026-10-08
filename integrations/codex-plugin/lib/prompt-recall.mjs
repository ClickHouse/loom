// Pure helpers for the loom-prompt-recall UserPromptSubmit hook (unit-tested).
//
// On by default for the first qualifying prompt of a session
// (LOOM_PROMPT_RECALL=first; `on` = every prompt, `off` = never). The memory-side sibling of prompt-pack.mjs:
// one context.pack per PROMPT, restricted to the planes a prompt can use
// mid-session (memories, decisions, procedures — the session primer already
// carried working memory, profile and the memory stack), gated on memory's
// own abstain and confidence, capped in characters, and never the same
// citations twice in a session. Per prompt, never per tool turn.
//
// Active retrieval as a hook: the model gets what memory knows about the
// prompt, tagged by plane, without having to ask. Pull (the agent calling
// memory.search itself) stays the correctness lever; this is the cost lever.

import { runIdFor } from "./run-hook.mjs";
export const RECALL_SECTIONS = ["memories", "decisions", "procedures"];
export const SEEN_CAP = 60;
export const DEFAULT_MAX_CHARS = 1200;

/** Gate on memory's own verdict: abstain or low confidence → silence. */
export function acceptPack(res, minConfidence = 0.5) {
  if (!res || !Array.isArray(res.blocks) || !res.blocks.length) return false;
  const meta = (res.section_meta && res.section_meta.memories) || null;
  if (meta) {
    if (meta.abstain) return false;
    if (Number(meta.retrieval_confidence ?? 0) < minConfidence) return false;
  }
  return res.blocks.some((b) => RECALL_SECTIONS.includes(b.section) && String(b.rendered || "").trim());
}

/**
 * Render the recall blocks whose citations the session has not all seen, in
 * pack order, tagged by plane label, at most `cap` blocks and `maxChars`
 * characters. Returns {text, seen}; text is "" when nothing is new.
 */
export function renderRecall(res, seen, { cap = 3, maxChars = DEFAULT_MAX_CHARS } = {}) {
  const seenSet = new Set(Array.isArray(seen) ? seen : []);
  const next = [...seenSet];
  const parts = [];
  for (const b of res.blocks || []) {
    if (!RECALL_SECTIONS.includes(b.section)) continue;
    const body = String(b.rendered || "").trim();
    if (!body) continue;
    const cites = Array.isArray(b.citations) ? b.citations.map(String) : [];
    if (cites.length && cites.every((c) => seenSet.has(c))) continue;
    for (const c of cites) {
      if (!seenSet.has(c)) {
        seenSet.add(c);
        next.push(c);
      }
    }
    const tag = b.label || b.section;
    parts.push(`<${tag}>\n${body}\n</${tag}>`);
    if (parts.length >= cap) break;
  }
  while (next.length > SEEN_CAP) next.shift();
  if (!parts.length) return { text: "", seen: next };
  const conf = Number(res.section_meta?.memories?.retrieval_confidence ?? 0).toFixed(2);
  let text =
    `Loom recall for this prompt (confidence ${conf}; blocks are tagged by plane, ` +
    `citations are memory keys — search memory yourself for anything deeper):\n${parts.join("\n")}`;
  if (text.length > maxChars) text = `${text.slice(0, Math.max(0, maxChars - 16)).trimEnd()}\n...[truncated]`;
  return { text, seen: next };
}

/** Body for the per-prompt context.pack. Carries the session id and the
 * run id the run hook uses (cc-<session_id>) so the pack's exposure row and
 * its out_procedure_keys can be joined to the session's judged outcome;
 * without them no pack names its session. */
export function recallBody(scope, payload, prompt, { budget = 600, topK = 3 } = {}) {
  const sessionId = String(payload?.session_id || payload?.sessionId || "").trim();
  const body = {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent,
    query: String(prompt || "").slice(0, 2000),
    mode: "task",
    token_budget: budget,
    top_k: topK,
    only_sections: RECALL_SECTIONS,
  };
  if (sessionId) {
    body.session_id = sessionId;
    body.run_id = runIdFor(sessionId);
  }
  return body;
}

export function recallPayload(text) {
  return {
    hookSpecificOutput: {
      hookEventName: "UserPromptSubmit",
      additionalContext: String(text || ""),
    },
  };
}
