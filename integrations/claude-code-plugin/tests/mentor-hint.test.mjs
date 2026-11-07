// The mentor-hint PostToolUse hook — lift a tool result's `hint` into context, once per code.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { decideShow, extractHint, hintPayload, isLoomTool, renderHint } from "../lib/mentor-hint.mjs";

test("isLoomTool: the loom MCP prefix, bare loom_ names, other servers ignored", () => {
  assert.equal(isLoomTool("mcp__loom__loom_memory_get"), true);
  assert.equal(isLoomTool("mcp__plugin_loom_loom__loom_search"), true);
  assert.equal(isLoomTool("loom_memory_set"), true);
  assert.equal(isLoomTool("mcp__github__create_issue"), false);
  assert.equal(isLoomTool("Read"), false);
  assert.equal(isLoomTool(""), false);
});

test("extractHint: structuredContent, content[0].text JSON, a bare object, a string; none without a hint", () => {
  const hint = { hint_code: "get_miss", hint: "no row at deploy_window: memory.search recalls by meaning" };
  assert.deepEqual(extractHint({ structuredContent: { found: false, ...hint } }), {
    code: "get_miss",
    text: hint.hint,
  });
  assert.deepEqual(extractHint({ content: [{ type: "text", text: JSON.stringify({ found: false, ...hint }) }] }), {
    code: "get_miss",
    text: hint.hint,
  });
  assert.deepEqual(extractHint({ ...hint }), { code: "get_miss", text: hint.hint });
  assert.deepEqual(extractHint(JSON.stringify(hint)), { code: "get_miss", text: hint.hint });
  // A hint without a code still shows, under a generic code.
  assert.equal(extractHint({ hint: "x" }).code, "hint");
  assert.equal(extractHint({ found: true, value: "v" }), null);
  assert.equal(extractHint({ content: [{ type: "text", text: "not json" }] }), null);
  assert.equal(extractHint(null), null);
  assert.equal(extractHint(42), null);
  // Bounded text.
  assert.equal(extractHint({ hint: "y".repeat(1000) }).text.length, 400);
});

test("decideShow: once per code, six per session", () => {
  let cache = {};
  const shown = [];
  for (const code of ["a", "a", "b", "c", "d", "e", "f", "g", "b"]) {
    const d = decideShow(cache, code);
    cache = d.cache;
    if (d.show) shown.push(code);
  }
  assert.deepEqual(shown, ["a", "b", "c", "d", "e", "f"]);
  assert.deepEqual(decideShow(null, "z"), { show: true, cache: { shown: ["z"] } });
  assert.deepEqual(decideShow({ shown: "junk" }, "z").cache, { shown: ["z"] });
});

test("renderHint + hintPayload: one line naming the code and the tool, as additionalContext only", () => {
  const line = renderHint(
    { code: "search_abstain_code", text: "memory abstained; code.search answers" },
    "mcp__loom__loom_search",
  );
  assert.equal(
    line,
    "Loom mentor (search_abstain_code, after loom_search): memory abstained; code.search answers — /loom-mentor has the table; mentor.brief the evidence.",
  );
  assert.equal(line.split("\n").length, 1);
  const payload = hintPayload(line);
  assert.equal(payload.hookSpecificOutput.hookEventName, "PostToolUse");
  assert.equal(payload.hookSpecificOutput.additionalContext, line);
  assert.equal(Object.keys(payload).length, 1); // never a permission decision
  assert.equal(hintPayload(""), null);
});
