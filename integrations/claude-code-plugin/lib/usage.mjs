// Pure helpers for the loom-usage Stop hook (unit-tested; no I/O).
//
// Claude Code writes the session transcript as JSONL; every assistant message
// carries the Messages API `usage` block. The hook reads the tail on each Stop,
// turns the assistant lines it has not posted yet into cost.record_turns rows,
// and the SERVER prices them. Rules: dedupe by message.id (several JSONL lines
// repeat one usage block while a message streams), skip sidechains (subagent
// transcripts have their own sessions), and never estimate — a turn without a
// usage block is not a row.

import { hookClient } from "./loom-client.mjs";

function n(v) {
  const x = Number(v);
  return Number.isFinite(x) && x > 0 ? Math.floor(x) : 0;
}

/**
 * Assistant turns with usage, deduped by message id, sidechains skipped,
 * in transcript order. `seen` is a Set of message ids already posted; the
 * returned `next` is the updated Set (bounded).
 */
export function extractTurns(objs, seen = new Set(), { cap = 200 } = {}) {
  const turns = [];
  const next = new Set(seen);
  for (const o of objs || []) {
    if (!o || o.type !== "assistant" || o.isSidechain) continue;
    const msg = o.message || {};
    const u = msg.usage;
    const id = String(msg.id || "");
    if (!u || !id || next.has(id)) continue;
    const model = String(msg.model || "");
    if (!model) continue;
    const cc = u.cache_creation || {};
    const cc1h = n(cc.ephemeral_1h_input_tokens);
    const ccTotal = n(u.cache_creation_input_tokens);
    // when the TTL split is absent, treat the whole write as 5-minute (the cheaper rate)
    const cc5m =
      cc.ephemeral_5m_input_tokens !== undefined ? n(cc.ephemeral_5m_input_tokens) : Math.max(0, ccTotal - cc1h);
    next.add(id);
    turns.push({
      message_id: id,
      model,
      input_tokens: n(u.input_tokens),
      cache_read_tokens: n(u.cache_read_input_tokens),
      cache_creation_5m_tokens: cc5m,
      cache_creation_1h_tokens: cc1h,
      output_tokens: n(u.output_tokens),
      turn_at: typeof o.timestamp === "string" ? o.timestamp : "",
    });
    if (turns.length >= cap) break;
  }
  // bound the memory: keep the most recent ids only
  while (next.size > 2000) next.delete(next.values().next().value);
  return { turns, next };
}

export function recordBody(scope, sessionId, turns, consumer = hookClient()) {
  return {
    org: scope.org,
    namespace: scope.namespace,
    agent: scope.agent || "-",
    user_id: scope.user_id || "-",
    session_id: String(sessionId || "nosession"),
    consumer,
    turns,
  };
}
