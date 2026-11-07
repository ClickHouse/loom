import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { blastSignature, decideEmit, renderBlast, blastPayload } from "../lib/blast-radius.mjs";

const IMPACT = {
  touched_symbols: [
    { qualified_name: "DB::MergeTreeData::renameTempPartAndAdd", path: "src/Storages/MergeTree/MergeTreeData.cpp" },
  ],
  impacted_symbols: [
    { qualified_name: "DB::MergeTreeSink::consume", path: "src/Storages/MergeTree/MergeTreeSink.cpp", hop: 1 },
    { qualified_name: "DB::MergeTreeSink::consume", path: "src/Storages/MergeTree/MergeTreeSink.cpp", hop: 1 },
    {
      qualified_name: "DB::ReplicatedMergeTreeSink::commitPart",
      path: "src/Storages/MergeTree/ReplicatedMergeTreeSink.cpp",
      hop: 1,
    },
  ],
  impacted_tests: [{ test_path: "tests/integration/test_merge_tree/test.py", src_path: "x", confidence: 0.7 }],
  co_change_missing: [
    { path: "src/Storages/MergeTree/MergeTreeData.h", prs: 14, of: 20 },
    { path: "src/Storages/StorageMergeTree.cpp", prs: 9, of: 20 },
  ],
};

test("renderBlast names callers, tests and untouched partners in one line", () => {
  const text = renderBlast("src/Storages/MergeTree/MergeTreeData.cpp", IMPACT, { maxNames: 5 });
  assert.match(
    text,
    /^Loom blast radius for src\/Storages\/MergeTree\/MergeTreeData.cpp \(edited MergeTreeData::renameTempPartAndAdd\)/,
  );
  assert.match(
    text,
    /3 callers in 2 files: MergeTreeSink::consume \(MergeTreeSink.cpp\), ReplicatedMergeTreeSink::commitPart/,
  );
  assert.match(text, /tests reaching it: 1 \(test.py\)/);
  assert.match(
    text,
    /usually changes with, not touched yet: src\/Storages\/MergeTree\/MergeTreeData.h \(14 PRs\), src\/Storages\/StorageMergeTree.cpp \(9 PRs\)/,
  );
  assert.match(text, /floors/);
  assert.ok(text.length < 520, `line stays short: ${text.length}`);
});

test("partners the session already touched are not reported as missing", () => {
  const text = renderBlast("src/Storages/MergeTree/MergeTreeData.cpp", IMPACT, {
    touchedPaths: ["src/Storages/MergeTree/MergeTreeData.h"],
  });
  assert.doesNotMatch(text, /MergeTreeData\.h/);
  assert.match(text, /StorageMergeTree\.cpp/);
});

test("empty radius renders nothing", () => {
  assert.equal(
    renderBlast("a.cpp", {
      touched_symbols: [{ qualified_name: "f" }],
      impacted_symbols: [],
      impacted_tests: [],
      co_change_missing: [],
    }),
    "",
  );
  assert.equal(renderBlast("a.cpp", null), "");
});

test("maxNames caps inline names and counts the rest", () => {
  const many = {
    ...IMPACT,
    impacted_symbols: Array.from({ length: 12 }, (_, i) => ({ qualified_name: `DB::C${i}::f`, path: `src/${i}.cpp` })),
  };
  const text = renderBlast("a.cpp", many, { maxNames: 2 });
  assert.match(text, /12 callers in 12 files: C0::f \(0.cpp\), C1::f \(1.cpp\) \+10 more/);
});

test("same touched set for a file is emitted once; a new set emits again", () => {
  const sig1 = blastSignature("a.cpp", IMPACT);
  let d = decideEmit({}, "a.cpp", sig1);
  assert.equal(d.emit, true);
  d = decideEmit(d.cache, "a.cpp", sig1);
  assert.equal(d.emit, false, "identical radius is not repeated");
  const sig2 = blastSignature("a.cpp", { touched_symbols: [{ qualified_name: "DB::Other" }] });
  d = decideEmit(d.cache, "a.cpp", sig2);
  assert.equal(d.emit, true, "editing a different function reports again");
  assert.equal(decideEmit(d.cache, "b.cpp", sig1).emit, true, "another file is independent");
});

test("payload is additional context for PostToolUse, never a decision", () => {
  const p = blastPayload("x");
  assert.equal(p.hookSpecificOutput.hookEventName, "PostToolUse");
  assert.equal(p.hookSpecificOutput.additionalContext, "x");
  assert.equal("permissionDecision" in p.hookSpecificOutput, false);
});

test("owners ride the line when present, but never make a radius on their own", () => {
  const withOwners = {
    ...IMPACT,
    owners: [
      { author: "alice", share: 0.6, prs: 4 },
      { author: "bob", share: 0.2, prs: 1 },
    ],
  };
  assert.match(renderBlast("a.cpp", withOwners), /who knows this code: alice \(4 PRs\), bob \(1 PRs\)/);
  const ownersOnly = {
    touched_symbols: [{ qualified_name: "f" }],
    impacted_symbols: [],
    impacted_tests: [],
    co_change_missing: [],
    owners: [{ author: "alice", prs: 2 }],
  };
  assert.equal(renderBlast("a.cpp", ownersOnly), "");
});

test("reverts touching the area are called out", () => {
  const withReverts = {
    ...IMPACT,
    reverts: [{ pr_number: 70001, title: "Revert x" }, { pr_number: 69990, title: "Revert y" }, { pr_number: 60000 }],
  };
  assert.match(
    renderBlast("a.cpp", withReverts),
    /reverted before: #70001, #69990 \+1 — check why before repeating it/,
  );
});

test("the true hop-1 total heads the list and the epistemic verdict closes the line", () => {
  const exact = { ...IMPACT, epistemic: "exact", by_hop_counts: { 1: 17 } };
  const text = renderBlast("a.cpp", exact);
  assert.match(text, /^Loom blast radius for a\.cpp \(edited MergeTreeData::renameTempPartAndAdd\):/);
  assert.match(text, /17 callers in 2 files: .* \+15 more/, "the true hop-1 total, not the page size");
  assert.match(text, /Counts are exact for static calls \(compiler-indexed\)\.$/);
});

test("a lower bound names its causes; a truncated page marks the file count", () => {
  const lb = {
    ...IMPACT,
    epistemic: "lower_bound",
    causes: ["unresolved_calls", "treesitter_only_files"],
    truncated: true,
    by_hop_counts: { 1: 3 },
  };
  const text = renderBlast("a.cpp", lb);
  assert.match(text, /3 callers in 2\+ files/);
  assert.match(text, /Counts are floors \(unresolved_calls, treesitter_only_files\)\.$/);
});

test("an empty walk stays silent, lower bound or not", () => {
  const empty = {
    touched_symbols: [{ qualified_name: "f" }],
    impacted_symbols: [],
    impacted_tests: [],
    co_change_missing: [],
  };
  assert.equal(renderBlast("a.cpp", empty), "");
  assert.equal(renderBlast("a.cpp", { ...empty, epistemic: "lower_bound", causes: ["treesitter_only_files"] }), "");
});
