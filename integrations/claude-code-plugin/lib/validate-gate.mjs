// Pure helpers for the loom-validate-gate Stop hook (unit-tested; no I/O).
//
// "Writes end on compile green." The loom-memory skill asks the agent to run
// `namespace.validate` before reporting done; left to the agent, it rarely
// happens. This hook makes the check the harness's job:
// on Stop, collect the memory keys this session wrote, ask `namespace.validate`
// for a report scoped to exactly those keys, and block the stop ONCE per
// distinct key set with the findings that name them — a conflict the session
// opened, a fact whose validity has ended, an attestation the edit made stale.
// The second stop on the same keys passes: the agent has seen the finding and
// either fixed it or chose not to. `info` findings never block.

import { decideBlock, gatePayload } from "./test-gate.mjs";

export { decideBlock, gatePayload };

// MCP tool names arrive as `mcp__loom__loom_memory_set`; the SDK/HTTP forms
// as `memory.set`. Match on the trailing verb only.
const WRITE_VERBS = new Set(["set", "update", "update_by_id", "apply"]);

/** True for a tool name that writes one memory key. */
export function isMemoryWrite(name) {
  const n = String(name || "")
    .trim()
    .toLowerCase()
    .replace(/\./g, "_")
    .replace(/^mcp__[a-z0-9_-]+?__/, "") // mcp__<server>__
    .replace(/^loom_/, "");
  const m = /^memory_([a-z_]+)$/.exec(n); // not working_memory_*
  return Boolean(m && WRITE_VERBS.has(m[1]));
}

/** Memory keys the session wrote, first-write order, deduped, capped: read
 *  from assistant tool_use blocks in the transcript (no server round-trip). */
export function writtenKeys(objs, { cap = 200 } = {}) {
  const seen = new Set();
  const out = [];
  for (const o of objs || []) {
    if (!o || o.type !== "assistant" || o.isSidechain) continue;
    const content = o.message && o.message.content;
    if (!Array.isArray(content)) continue;
    for (const b of content) {
      if (!b || b.type !== "tool_use" || !isMemoryWrite(b.name)) continue;
      const k = b.input && b.input.memory_key;
      if (typeof k !== "string" || !k.trim() || seen.has(k)) continue;
      seen.add(k);
      out.push(k);
      if (out.length >= cap) return out;
    }
  }
  return out;
}

/** Stable signature of the key set (order-free). */
export function keysSignature(keys) {
  return [...new Set((keys || []).map(String).filter(Boolean))].sort().join("\n");
}

function firstSentence(text, max = 110) {
  const s = String(text || "")
    .split(/(?<=\.)\s/)[0]
    .trim()
    .replace(/\.$/, "");
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s;
}

/**
 * Render the block text, or "" when nothing blocks: findings with count > 0
 * whose severity is error or warning, each naming its check, up to
 * `maxKeys` sample keys and the first sentence of its remedy. Bounded to
 * `maxChecks` checks.
 */
export function renderValidate(report, { maxKeys = 4, maxChecks = 4 } = {}) {
  if (!report || !Array.isArray(report.findings)) return "";
  const hits = report.findings.filter((f) => f && Number(f.count) > 0 && f.severity !== "info");
  if (!hits.length) return "";
  const order = { error: 0, warning: 1 };
  hits.sort((a, b) => (order[a.severity] ?? 2) - (order[b.severity] ?? 2) || Number(b.count) - Number(a.count));
  const parts = [];
  for (const f of hits.slice(0, maxChecks)) {
    const keys = [...new Set((f.samples || []).map((s) => String((s && s.memory_key) || "")).filter(Boolean))];
    const shown = keys.slice(0, maxKeys);
    const more = Number(f.count) - shown.length;
    const remedy = firstSentence(f.detail);
    parts.push(`${f.check}: ${shown.join(", ")}${more > 0 ? ` (+${more})` : ""}${remedy ? ` — ${remedy}` : ""}`);
  }
  const extra = hits.length - Math.min(hits.length, maxChecks);
  const total = hits.reduce((n, f) => n + Number(f.count), 0);
  const scoped = Number(report.keys_scoped) > 0 ? ` on the ${report.keys_scoped} key(s) this session wrote` : "";
  return (
    `Loom validate gate: ${total} finding(s)${scoped} · ${parts.join(" · ")}` +
    `${extra > 0 ? ` (+${extra} more checks)` : ""}. ` +
    "Fix them (same key), or say why not, before finishing."
  );
}
