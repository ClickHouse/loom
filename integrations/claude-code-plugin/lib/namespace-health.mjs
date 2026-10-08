// Pure helpers for the loom-namespace-health Stop hook (unit-tested; no I/O).
//
// The server's diagnosis of a misused namespace lives where the misbehaving
// agent never looks: a response field on memory.set and an op it has to choose
// to call. A namespace can collect thousands of exact-key gets against a few
// dozen searches, and hundreds of double-encoded values, without the agent ever
// being told. This hook is the store telling it at session end: on the first
// Stop of a session, one `namespace.audit` over the last day with no samples,
// and, when it carries a critical finding, ONE line back to the agent naming
// the codes and the skill that reworks the namespace. A Stop hook's stdout at
// exit 0 reaches the transcript viewer, not the model; the block-once reason is
// the one channel the harness gives a hook to the agent, so the line rides it —
// once per session, and never on a stop the hook itself caused. Fail-open: no
// token, no scope, server down or slow, a healthy namespace — nothing is
// printed and the agent stops as normal.

export const AUDIT_WINDOW_SECONDS = 86400;
export const AUDIT_BODY = Object.freeze({ window_seconds: AUDIT_WINDOW_SECONDS, sample_limit: 0 });
const MAX_CODES = 5;

/** Body for namespace.audit: the session's namespace, one day, no samples. */
export function auditBody(scope) {
  return {
    org: scope.org,
    namespace: scope.namespace,
    ...AUDIT_BODY,
    consumer: "claude-code-namespace-health",
  };
}

/** The critical finding codes of an audit report, in report order; [] when
 *  the report is missing, malformed, or has none. */
export function criticalCodes(report) {
  if (!report || !Array.isArray(report.findings)) return [];
  return report.findings
    .filter((f) => f && f.severity === "critical" && Number(f.count) > 0)
    .map((f) => String(f.code || ""))
    .filter(Boolean);
}

/** The one line, or "" when there is nothing to say. */
export function renderHealth(report, namespace) {
  const codes = criticalCodes(report);
  if (!codes.length) return "";
  const shown = codes.slice(0, MAX_CODES);
  const more = codes.length - shown.length;
  const ns = String(namespace || (report && report.namespace) || "").trim() || "?";
  return (
    `loom: namespace ${ns} has ${codes.length} critical usage finding${codes.length === 1 ? "" : "s"} ` +
    `(${shown.join(", ")}${more > 0 ? `, +${more}` : ""}) — run /loom-optimize`
  );
}

/** Audit once per session: the first Stop runs it and records that it did;
 *  every later Stop passes without a call. */
export function decideAudit(cache) {
  if (cache && cache.audited_at) return { audit: false, cache };
  return { audit: true, cache: { ...(cache || {}), audited_at: Date.now() } };
}

/** Stop-hook payload: the line as a block-once reason; empty text = no output. */
export function healthPayload(text) {
  return text ? { decision: "block", reason: text } : null;
}
