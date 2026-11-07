import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BLOCK_CHAR_LIMIT,
  BOOKMARK_FRAME,
  blockKeyFor,
  buildSessionState,
  editedFiles,
  firstUserPrompt,
  parseGitStatus,
  setBody,
  stateFingerprint,
} from "../lib/working-memory.mjs";

const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: ["project:loom"] };

const transcript = [
  { type: "user", message: { role: "user", content: "Fix the fold-back worker so recall_count moves" } },
  {
    type: "assistant",
    message: {
      content: [
        { type: "text", text: "Looking at the worker." },
        { type: "tool_use", name: "Read", input: { file_path: "/repo/server/workers/usage_fold.py" } },
      ],
    },
  },
  { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t1", content: "…" }] } },
  {
    type: "assistant",
    message: {
      content: [
        { type: "tool_use", name: "Edit", input: { file_path: "/repo/server/workers/usage_fold.py" } },
        { type: "tool_use", name: "Write", input: { file_path: "/repo/tests/unit/test_sql_alias_shadow.py" } },
        { type: "tool_use", name: "Edit", input: { file_path: "/repo/server/workers/usage_fold.py" } },
      ],
    },
  },
  {
    type: "assistant",
    isSidechain: true,
    message: { content: [{ type: "tool_use", name: "Write", input: { file_path: "/repo/side.py" } }] },
  },
  { type: "assistant", message: { content: [{ type: "text", text: "Renamed the alias; tests green." }] } },
];

test("blockKeyFor is one note per project and safe as a key", () => {
  assert.equal(blockKeyFor("loom"), "session:loom");
  assert.equal(blockKeyFor("my repo/with spaces"), "session:my_repo_with_spaces");
  assert.equal(blockKeyFor(""), "session:project");
});

test("firstUserPrompt skips tool_result rows and sidechains", () => {
  assert.equal(firstUserPrompt(transcript), "Fix the fold-back worker so recall_count moves");
  assert.equal(firstUserPrompt([{ type: "user", message: { content: [{ type: "tool_result" }] } }]), "");
  assert.equal(firstUserPrompt([]), "");
});

test("editedFiles lists edit tools only, first-touch order, deduped, sidechains skipped", () => {
  assert.deepEqual(editedFiles(transcript), [
    "/repo/server/workers/usage_fold.py",
    "/repo/tests/unit/test_sql_alias_shadow.py",
  ]);
});

test("parseGitStatus reads the branch header and the change lines", () => {
  const st = parseGitStatus("## fix/alias...origin/fix/alias [ahead 1]\n M server/a.py\n?? tests/new.py\n");
  assert.equal(st.branch, "fix/alias");
  assert.deepEqual(st.changed, [" M server/a.py", "?? tests/new.py"]);
  assert.deepEqual(parseGitStatus(""), { branch: "", changed: [] });
});

test("buildSessionState renders goal, tree, edits and last text, paths relative to cwd", () => {
  const body = buildSessionState({
    goal: "Fix the fold-back worker",
    files: editedFiles(transcript),
    git: { branch: "main", changed: [" M server/workers/usage_fold.py"] },
    lastText: "Renamed the alias;\n  tests green.",
    cwd: "/repo",
    writtenAt: "2026-09-12T08:00:00Z",
    reason: "PreCompact",
  });
  assert.match(body, /^# Session state — written by the PreCompact hook at 2026-09-12T08:00:00Z\n/);
  assert.match(body, /\ngoal: Fix the fold-back worker\n/);
  assert.match(body, /\nbranch: main — 1 uncommitted change\n {3}M server\/workers\/usage_fold\.py\n/);
  assert.match(
    body,
    /\nedited this session: server\/workers\/usage_fold\.py, tests\/unit\/test_sql_alias_shadow\.py\n/,
  );
  assert.match(body, /\nlast reply \(the previous turn's words, not a fact\): Renamed the alias; tests green\.$/);
  assert.equal(body.split("\n")[1], BOOKMARK_FRAME, "the frame is the line under the title");
});

test("buildSessionState shortens a Windows payload path under the cwd", { skip: process.platform !== "win32" }, () => {
  const body = buildSessionState({
    goal: "g",
    files: ["C:\\repo\\src\\a.py"],
    git: null,
    lastText: "",
    cwd: "C:/repo",
    writtenAt: "t",
    reason: "Stop",
  });
  assert.match(body, /\nedited this session: src\/a\.py$/m);
});

test("buildSessionState is bounded and omits empty sections", () => {
  const body = buildSessionState({
    goal: "x".repeat(5000),
    files: [],
    git: null,
    lastText: "y".repeat(5000),
    writtenAt: "t",
  });
  assert.ok(body.length <= BLOCK_CHAR_LIMIT);
  assert.ok(!body.includes("branch:"));
  assert.ok(!body.includes("edited this session"));
  assert.match(body, /goal: x{300}…/);
});

test("setBody is agent-scoped so context.pack loads it at the next SessionStart", () => {
  const b = setBody(scope, "loom", "note");
  assert.deepEqual(b, {
    org: "o",
    namespace: "coding",
    agent: "claude-code",
    block_key: "session:loom",
    content: "note",
    char_limit: BLOCK_CHAR_LIMIT,
  });
  assert.equal(b.session_id, undefined);
});

test("stateFingerprint ignores the timestamp line so an unchanged turn writes nothing", () => {
  const a = buildSessionState({ goal: "g", lastText: "l", writtenAt: "t1" });
  const b = buildSessionState({ goal: "g", lastText: "l", writtenAt: "t2" });
  assert.notEqual(a, b);
  assert.equal(stateFingerprint(a), stateFingerprint(b));
  const c = buildSessionState({ goal: "g", lastText: "changed", writtenAt: "t2" });
  assert.notEqual(stateFingerprint(a), stateFingerprint(c));
});

// --- run scratchpad -----------------------------------------
import { SCRATCHPAD_BLOCK, SCRATCHPAD_LINE_MAX, scratchpadAppendBody, turnConclusion } from "../lib/working-memory.mjs";

test("turnConclusion collapses whitespace and stops at the first sentence past 40 chars", () => {
  const text = "Fixed the flaky retry test by pinning the clock.\n\nNext I will look at the\tcache. Then more.";
  assert.equal(turnConclusion(text), "Fixed the flaky retry test by pinning the clock.");
  assert.equal(turnConclusion("Done."), "Done.");
  assert.equal(turnConclusion("   "), "");
});

test("turnConclusion caps a sentence-less wall of text with an ellipsis", () => {
  const out = turnConclusion("x".repeat(1000));
  assert.equal(out.length, SCRATCHPAD_LINE_MAX);
  assert.ok(out.endsWith("…"));
});

test("scratchpadAppendBody appends one timestamped line at run scope", () => {
  const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom" };
  const body = scratchpadAppendBody(scope, "cc-abc", "picked RRF over semantic", "2026-09-12T10:02:33.000Z");
  assert.deepEqual(body, {
    org: "o",
    namespace: "coding",
    agent: "claude-code",
    run_id: "cc-abc",
    block_key: SCRATCHPAD_BLOCK,
    addition: "- 10:02Z picked RRF over semantic",
    separator: "\n",
  });
  assert.equal(scratchpadAppendBody(scope, "", "x", "2026-09-12T10:02:33.000Z").run_id, "-");
  assert.ok(!("session_id" in body), "session axis stays '-' like the session note");
});

test("firstUserPrompt keeps a pasted image as a placeholder beside its text", () => {
  const png = { type: "image", source: { type: "base64", media_type: "image/png", data: "AAAA" } };
  const objs = [{ type: "user", message: { content: [{ type: "text", text: "what does this show?" }, png] } }];
  assert.equal(firstUserPrompt(objs), "what does this show?\n[image: image/png]");
});

test("the session-state note carries no secret from the goal or the last reply", () => {
  const secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8";
  const note = buildSessionState({ goal: `deploy with ${secret}`, lastText: `used ${secret} to push` });
  const line = scratchpadAppendBody({ org: "o", namespace: "n", agent: "a" }, "r", `pushed with ${secret}`).addition;
  for (const text of [note, line]) {
    assert.ok(!text.includes(secret.slice(4)), text);
    assert.ok(text.includes("[redacted]"), text);
  }
});
