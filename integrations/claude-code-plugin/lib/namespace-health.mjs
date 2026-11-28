// Pure helpers for the loom-namespace-health Stop hook (unit-tested; no I/O).
//
// The server's diagnosis of a misused namespace lives where the misbehaving
// agent never looks: a response field on memory.set and an op it has to choose
// to call. A namespace can collect thousands of exact-key gets against a few
// dozen searches, and hundreds of double-encoded values, without the agent ever
// being told. This hook is the store telling it: once per session, one
// `namespace.audit` over the last day with no samples, and, when it carries a
// critical finding, ONE line back to the agent at a Stop, naming the codes and
// the skill that reworks the namespace. A Stop hook's stdout at exit 0 reaches
// the transcript viewer, not the model; the block-once reason is the one
// channel the harness gives a hook to the agent, so the line rides it — once
// per session, and never on a stop the hook itself caused. Fail-open: no
// token, no scope, server down or slow, a healthy namespace — nothing is
// printed and the agent stops as normal.
//
// The session's state file says how far that has got:
//   {asked_at}              a detached copy is asking (started at a prompt);
//                           past twice the request bound it died before its
//                           answer
//   {asked_at, line}        it has the verdict: the line, or "" for none
//   {..., audited_at}       a Stop has told the agent, or had nothing to tell

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

/** A prompt's detached copy asks once per session, and not after a Stop has. */
export function decideAsk(state) {
  if (state && (state.asked_at || state.audited_at)) return { ask: false, state };
  return { ask: true, state: { ...(state || {}), asked_at: Date.now() } };
}

/** What a Stop does, once per session. A verdict a detached copy stored is
 *  told (`line`) with no call; while that copy is still asking the Stop
 *  passes, and a later one tells. With no copy at work (a wiring that starts
 *  this script at Stop only), or one that asked more than `overdueMs` ago,
 *  longer than a live copy takes to answer or give up, and stored no verdict
 *  (it died before its answer), this Stop asks (`audit`). `cache` is the
 *  state to keep: the one given when nothing changed. */
export function decideAudit(state, overdueMs, now = Date.now()) {
  if (state && state.audited_at) return { audit: false, cache: state };
  if (state && typeof state.line === "string") {
    return { audit: false, line: state.line, cache: { ...state, audited_at: now } };
  }
  if (state && state.asked_at && now - state.asked_at <= overdueMs) return { audit: false, cache: state };
  return { audit: true, cache: { ...(state || {}), audited_at: now } };
}

/** Stop-hook payload: the line as a block-once reason; empty text = no output. */
export function healthPayload(text) {
  return text ? { decision: "block", reason: text } : null;
}
