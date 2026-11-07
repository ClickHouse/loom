// Decision accounting for the code shunt: pure helpers, the hook does the I/O.
//
// Every decision for a bulk-read-shaped call inside an indexed repository,
// hit (denied) or pass (allowed), is one TSV line in a local pending log.
// Batches flush to Loom as code.outcome rows (shunt_hit, and one shunt_pass
// per pass reason), so tokens kept out and let in are a production series.

import { createHash } from "node:crypto";

import { PASS_REASONS, readSpan } from "./code-shunt.mjs";

export const SHUNT_FLUSH_EVERY = 50;
/** `source` of a hit row and of a pass line with no known pass reason. */
export const SHUNT_CLIENT_SOURCE = "claude-code-shunt";

/** The tmp-state kind every pending batch of one reporting scope shares: the
 *  server, the token (hashed) and the repository's org/namespace, so a flush
 *  posts only batches it would have posted itself. */
export function batchKind({ serverUrl = "", token = "", org = "", namespace = "" } = {}) {
  const tokenId = createHash("sha256").update(String(token)).digest("hex").slice(0, 16);
  const scope = createHash("sha256").update(`${serverUrl}|${tokenId}|${org}/${namespace}`).digest("hex").slice(0, 12);
  return `shunt-pending-${scope}`;
}

/** Tokens the model did not ingest because a hit denied the read: bytes/4
 *  (the same estimate bench/code_shunt_tokens.py uses). 0 when unknown
 *  (a tree-wide grep's output size is not knowable before it runs). */
export function estimateTokensSaved(bytes) {
  const n = Number(bytes);
  return Number.isFinite(n) && n > 0 ? Math.floor(n / 4) : 0;
}

/** Tokens a PASSED Read puts into the model's context: the file's bytes/4
 *  scaled to the share of lines the span covers (bytes/4 is within 3.4% for
 *  served JSON; here it is the raw file, the same figure estimateTokensSaved
 *  uses for a hit, so hit and pass tokens add up). This is what the shunt
 *  report sums per pass reason. */
export function estimateTokensRead({ bytes, lineCount, offset, limit }) {
  const b = Number(bytes);
  if (!Number.isFinite(b) || b <= 0) return 0;
  if (offset == null && limit == null) return estimateTokensSaved(b);
  const { start, end } = readSpan({ lineCount, offset, limit });
  if (!start) return 0;
  return Math.floor((b * (end - start + 1)) / Number(lineCount) / 4);
}

/** Field sanitiser for the TSV log: one line, no tabs, bounded. */
function cell(v, max) {
  return String(v ?? "")
    .replace(/[\t\r\n]+/g, " ")
    .slice(0, max);
}

/** One log line: ISO ts, decision, tool, tokens, reason head, pass reason,
 *  path, start line, end line — tab-separated, no tabs inside a cell. */
export function formatShuntLine({
  decision,
  tool,
  tokensSaved = 0,
  reason = "",
  ts,
  passReason = "",
  path = "",
  startLine = 0,
  endLine = 0,
}) {
  const d = decision === "hit" ? "hit" : "pass";
  const head = cell(String(reason || "").split(" — ")[0], 200);
  const pr = d === "pass" && PASS_REASONS.includes(passReason) ? passReason : "";
  const cells = [
    ts || new Date().toISOString(),
    d,
    cell(tool || "?", 32),
    Math.max(0, Math.floor(Number(tokensSaved) || 0)),
    head,
    pr,
    cell(path, 240),
    Math.max(0, Math.floor(Number(startLine) || 0)),
    Math.max(0, Math.floor(Number(endLine) || 0)),
  ];
  return cells.join("\t") + "\n";
}

/** `path:start-end` for a decision's span, `path` alone when the lines are
 *  unknown, "" when nothing is known — the shape CodeOutcomeInput.spans takes. */
export function spanString(path, startLine, endLine) {
  const p = String(path || "");
  const a = Math.floor(Number(startLine) || 0);
  const b = Math.floor(Number(endLine) || 0);
  if (a > 0 && b >= a) return p ? `${p}:${a}-${b}` : `${a}-${b}`;
  return p;
}

/** Fold pending log text into code.outcome payloads: ONE shunt_hit body
 *  (source = the client) and one shunt_pass body PER PASS REASON (source =
 *  the reason), each with `targets` (one tool name per decision) and
 *  `spans` aligned with it (`path:start-end`, "" unknown); tokens summed.
 *  A pass line with no known reason folds under the client source.
 *  Malformed lines (not nine cells) are skipped, never thrown on. `passes` is ordered by
 *  reason so a batch flushes deterministically. */
export function aggregateShuntLog(text) {
  const mk = () => ({ count: 0, tokens: 0, tools: [], spans: [] });
  const hit = mk();
  const passes = new Map();
  for (const line of String(text || "").split("\n")) {
    if (!line) continue;
    const cells = line.split("\t");
    if (cells.length !== 9) continue;
    const [, decision, tool, tokens, , passReason, path, start, end] = cells;
    let side;
    if (decision === "hit") side = hit;
    else if (decision === "pass") {
      const key = PASS_REASONS.includes(passReason) ? passReason : SHUNT_CLIENT_SOURCE;
      side = passes.get(key) || passes.set(key, mk()).get(key);
    } else continue;
    side.count += 1;
    side.tokens += Math.max(0, Number(tokens) || 0);
    if (side.tools.length < 50) {
      side.tools.push(tool || "?");
      side.spans.push(spanString(path, start, end));
    }
  }
  const body = (side, action, source) =>
    side.count
      ? { action, trace_run_id: "", targets: side.tools, spans: side.spans, tokens_saved: side.tokens, source }
      : null;
  const passBodies = [...passes.keys()].sort().map((k) => body(passes.get(k), "shunt_pass", k));
  const lines = hit.count + [...passes.values()].reduce((n, side) => n + side.count, 0);
  return { hit: body(hit, "shunt_hit", SHUNT_CLIENT_SOURCE), passes: passBodies, lines };
}

/** Flush policy: a deny already paid a server round trip, so piggy-back on
 *  it; otherwise only once the backlog is worth a request. Pass-throughs
 *  therefore never add network latency by themselves. */
export function shouldFlushShunt({ denied, pending }) {
  return Boolean(denied) || pending >= SHUNT_FLUSH_EVERY;
}
