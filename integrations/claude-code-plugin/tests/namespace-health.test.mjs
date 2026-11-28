// The namespace-health hook — one audit per session, one line on a critical.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AUDIT_WINDOW_SECONDS,
  auditBody,
  criticalCodes,
  decideAsk,
  decideAudit,
  healthPayload,
  renderHealth,
} from "../lib/namespace-health.mjs";

const finding = (code, severity, count = 1) => ({ code, severity, count });

test("auditBody: the session's namespace, one day, no samples", () => {
  const body = auditBody({ org: "acme", namespace: "sre" });
  assert.equal(body.org, "acme");
  assert.equal(body.namespace, "sre");
  assert.equal(body.window_seconds, 86400);
  assert.equal(AUDIT_WINDOW_SECONDS, 86400);
  assert.equal(body.sample_limit, 0);
  assert.equal(body.consumer, "claude-code-namespace-health");
});

test("criticalCodes: criticals with a count, in report order; warnings, infos and junk ignored", () => {
  const report = {
    findings: [
      finding("hash_keys", "warning", 599),
      finding("exact_key_store", "critical", 6606),
      finding("version_churn", "info", 0),
      finding("double_encoded_values", "critical", 760),
      finding("coordination_as_memory", "critical", 0),
      null,
      { severity: "critical", count: 3 },
    ],
  };
  assert.deepEqual(criticalCodes(report), ["exact_key_store", "double_encoded_values"]);
  assert.deepEqual(criticalCodes(null), []);
  assert.deepEqual(criticalCodes({ findings: "nope" }), []);
  assert.deepEqual(criticalCodes({ healthy: true, findings: [] }), []);
});

test("renderHealth: one line naming the namespace, the count, the codes and the skill", () => {
  const report = {
    namespace: "from-report",
    findings: [finding("exact_key_store", "critical", 6606), finding("double_encoded_values", "critical", 760)],
  };
  assert.equal(
    renderHealth(report, "clickhouse/ops-alerts"),
    "loom: namespace clickhouse/ops-alerts has 2 critical usage findings (exact_key_store, double_encoded_values) — run /loom-optimize",
  );
  assert.equal(
    renderHealth({ findings: [finding("unembedded_rows", "critical")] }, "ns"),
    "loom: namespace ns has 1 critical usage finding (unembedded_rows) — run /loom-optimize",
  );
  // The report's namespace is the fallback for the scope's.
  assert.match(renderHealth(report, ""), /^loom: namespace from-report has 2/);
  // One line, whatever the report holds.
  assert.equal(renderHealth(report, "ns").split("\n").length, 1);
});

test("renderHealth: silence on a healthy namespace, a warning-only one, an error or no report", () => {
  assert.equal(renderHealth({ healthy: true, findings: [finding("hash_keys", "warning", 10)] }, "ns"), "");
  assert.equal(renderHealth({ findings: [] }, "ns"), "");
  assert.equal(renderHealth(null, "ns"), ""); // postBounded returned null: server down / slow / 4xx
  assert.equal(renderHealth({ error: "boom" }, "ns"), "");
  assert.equal(healthPayload(""), null);
});

test("renderHealth: many codes are capped with a +N tail", () => {
  const report = {
    findings: ["a", "b", "c", "d", "e", "f", "g"].map((c) => finding(c, "critical", 2)),
  };
  assert.equal(
    renderHealth(report, "ns"),
    "loom: namespace ns has 7 critical usage findings (a, b, c, d, e, +2) — run /loom-optimize",
  );
});

test("decideAudit: with no copy at work the first stop audits, every later one passes", () => {
  const first = decideAudit({});
  assert.equal(first.audit, true);
  assert.ok(first.cache.audited_at > 0);
  const second = decideAudit(first.cache);
  assert.equal(second.audit, false);
  assert.equal(second.cache, first.cache);
  assert.equal(decideAudit(null).audit, true);
});

test("decideAsk: a prompt's copy asks once per session, and not after a stop has", () => {
  const first = decideAsk({});
  assert.equal(first.ask, true);
  assert.ok(first.state.asked_at > 0);
  assert.equal(decideAsk(first.state).ask, false);
  assert.equal(decideAsk({ ...first.state, line: "" }).ask, false);
  assert.equal(decideAsk(decideAudit({}).cache).ask, false);
  assert.equal(decideAsk(null).ask, true);
});

test("decideAudit: a stop passes while a copy is asking, tells its verdict once, and never audits", () => {
  const asking = decideAsk({}).state;
  const pending = decideAudit(asking);
  assert.deepEqual([pending.audit, pending.line], [false, undefined]);
  assert.equal(pending.cache, asking, "nothing to write: a later stop still tells");

  const line = "loom: namespace ns has 1 critical usage finding (x) — run /loom-optimize";
  const told = decideAudit({ ...asking, line });
  assert.deepEqual([told.audit, told.line], [false, line]);
  assert.ok(told.cache.audited_at > 0);
  const after = decideAudit(told.cache);
  assert.deepEqual([after.audit, after.line], [false, undefined]);

  // A healthy namespace, or an audit that did not answer: nothing to tell, once.
  const quiet = decideAudit({ ...asking, line: "" });
  assert.deepEqual([quiet.audit, quiet.line], [false, ""]);
  assert.ok(quiet.cache.audited_at > 0);
});

test("healthPayload: the line rides a block-once reason, nothing else", () => {
  assert.deepEqual(healthPayload("loom: namespace ns has 1 critical usage finding (x) — run /loom-optimize"), {
    decision: "block",
    reason: "loom: namespace ns has 1 critical usage finding (x) — run /loom-optimize",
  });
});
