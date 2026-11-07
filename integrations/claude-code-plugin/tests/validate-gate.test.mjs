// The validate gate — keys the session wrote, scoped report, one block.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  decideBlock,
  gatePayload,
  isMemoryWrite,
  keysSignature,
  renderValidate,
  writtenKeys,
} from "../lib/validate-gate.mjs";

function turn(blocks, extra = {}) {
  return { type: "assistant", message: { content: blocks }, ...extra };
}
const use = (name, input) => ({ type: "tool_use", name, input });

test("isMemoryWrite: MCP, SDK and HTTP spellings of the four write verbs, nothing else", () => {
  for (const n of [
    "mcp__loom__loom_memory_set",
    "loom_memory_update_by_id",
    "memory.set",
    "memory_apply",
    "memory.update",
  ]) {
    assert.equal(isMemoryWrite(n), true, n);
  }
  for (const n of ["mcp__loom__loom_memory_get", "memory.search", "memory_delete", "working_memory_set", "Edit", ""]) {
    assert.equal(isMemoryWrite(n), false, n);
  }
});

test("writtenKeys: first-write order, deduped, sidechains and non-writes skipped, capped", () => {
  const objs = [
    { type: "user", message: { content: "hi" } },
    turn([use("mcp__loom__loom_memory_set", { memory_key: "deploy_window", value: "Tue" })]),
    turn([use("mcp__loom__loom_memory_get", { memory_key: "other" }), use("Edit", { file_path: "a.py" })]),
    turn([use("mcp__loom__loom_memory_set", { memory_key: "db_engine" })], { isSidechain: true }),
    turn([
      use("mcp__loom__loom_memory_update_by_id", { memory_key: "deploy_window" }),
      use("mcp__loom__loom_memory_apply", { memory_key: " " }),
    ]),
    turn([use("mcp__loom__loom_memory_set", { memory_key: 42 })]),
    turn([use("mcp__loom__loom_memory_set", { memory_key: "pr_owner" })]),
    { type: "assistant", message: { content: "plain text" } },
  ];
  assert.deepEqual(writtenKeys(objs), ["deploy_window", "pr_owner"]);
  assert.deepEqual(writtenKeys(objs, { cap: 1 }), ["deploy_window"]);
  assert.deepEqual(writtenKeys([]), []);
  assert.deepEqual(writtenKeys(null), []);
});

test("keysSignature is order-free and deduped; decideBlock blocks once per signature", () => {
  assert.equal(keysSignature(["b", "a", "b"]), keysSignature(["a", "b"]));
  let d = decideBlock({}, keysSignature(["a", "b"]));
  assert.equal(d.block, true);
  d = decideBlock(d.cache, keysSignature(["b", "a"]));
  assert.equal(d.block, false); // same keys, second stop passes
  d = decideBlock(d.cache, keysSignature(["a", "b", "c"]));
  assert.equal(d.block, true); // a new key written since → gate again
});

const REPORT = {
  keys_scoped: 3,
  findings: [
    {
      check: "unsourced_low_confidence",
      severity: "info",
      count: 2,
      samples: [{ memory_key: "guess" }],
      detail: "An inference.",
    },
    {
      check: "stale_valid_to",
      severity: "warning",
      count: 1,
      samples: [{ memory_key: "db_engine", valid_to: "2026-01-01" }],
      detail: "An active fact whose own stated validity (valid_to) has ended. It is still ranked as current.",
    },
    {
      check: "divergent_status",
      severity: "error",
      count: 1,
      samples: [{ memory_key: "deploy_window", session_id: "s" }],
      detail: "loom.memory and loom.memory_embeddings disagree about a row's status. Run reconcile.",
    },
    { check: "conflicted_aging", severity: "warning", count: 0, samples: [], detail: "x" },
  ],
};

test("renderValidate: errors first, info and zero counts excluded, keys and remedy named", () => {
  const text = renderValidate(REPORT);
  assert.match(text, /^Loom validate gate: 2 finding\(s\) on the 3 key\(s\) this session wrote/);
  assert.ok(text.indexOf("divergent_status: deploy_window") < text.indexOf("stale_valid_to: db_engine"));
  assert.match(text, /disagree about a row's status · stale_valid_to/);
  assert.doesNotMatch(text, /unsourced_low_confidence|conflicted_aging|guess/);
  assert.match(text, /before finishing\.$/);
});

test("renderValidate: sample keys capped with a +n, checks capped, empty when nothing blocks", () => {
  const many = {
    findings: [
      {
        check: "stale_verification",
        severity: "warning",
        count: 9,
        samples: ["a", "b", "c", "d", "e"].map((k) => ({ memory_key: k })),
        detail: "Re-verify.",
      },
      { check: "expired_live", severity: "warning", count: 1, samples: [{ memory_key: "z" }], detail: "" },
    ],
  };
  const text = renderValidate(many, { maxKeys: 2, maxChecks: 1 });
  assert.match(text, /stale_verification: a, b \(\+7\) — Re-verify \(\+1 more checks\)\./);
  assert.match(text, /\(\+1 more checks\)/);
  assert.doesNotMatch(text, /expired_live/);
  assert.equal(renderValidate({ findings: [{ check: "x", severity: "info", count: 5, samples: [] }] }), "");
  assert.equal(renderValidate({ findings: [] }), "");
  assert.equal(renderValidate(null), "");
  assert.equal(gatePayload(""), null);
  assert.deepEqual(gatePayload("t"), { decision: "block", reason: "t" });
});
