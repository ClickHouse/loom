import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appendRecalledStash,
  beginBody,
  completeBody,
  lastAssistantText,
  readRecalledStash,
  retainFor,
  runIdFor,
  servedMemoryKeys,
  statusFor,
} from "../lib/run-hook.mjs";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: ["project:loom"] };

test("runIdFor derives a stable ledger key from the session", () => {
  assert.equal(runIdFor("abc-123"), "cc-abc-123");
  assert.equal(runIdFor(""), "");
  assert.equal(runIdFor(undefined), "");
});

test("beginBody uses the first prompt as the goal, bounded", () => {
  const long = "x".repeat(9000);
  const b = beginBody(scope, { session_id: "s1", cwd: "/repo" }, long);
  assert.equal(b.run_id, "cc-s1");
  assert.equal(b.session_id, "s1");
  assert.equal(b.goal.length, 2000);
  assert.equal(b.user_request.length, 8000);
  assert.equal(b.environment, "claude-code");
  assert.equal(b.metadata.project, "loom");
  assert.equal(beginBody(scope, { session_id: "s1" }, "   ").goal, "(empty prompt)");
});

test("completeBody never credits memories and maps the end reason", () => {
  const c = completeBody(scope, { session_id: "s1", reason: "clear" }, "Done: pinned the image.");
  assert.equal(c.run_id, "cc-s1");
  assert.equal(c.status, "completed");
  assert.equal(c.outcome, "none");
  assert.equal(c.final_response, "Done: pinned the image.");
  assert.equal(c.metadata.session_end_reason, "clear");
  assert.equal(c.memories_used, undefined);
  assert.equal(statusFor("error"), "failed");
  assert.equal(statusFor("prompt_input_exit"), "completed");
});

test("lastAssistantText picks the final non-sidechain text block", () => {
  const objs = [
    { type: "user", message: { content: "hi" } },
    { type: "assistant", message: { content: [{ type: "text", text: "first" }] } },
    { type: "assistant", isSidechain: true, message: { content: [{ type: "text", text: "subagent" }] } },
    { type: "assistant", message: { content: [{ type: "tool_use", name: "Bash" }] } },
    {
      type: "assistant",
      message: {
        content: [
          { type: "text", text: " last  " },
          { type: "text", text: "line2" },
        ],
      },
    },
  ];
  assert.equal(lastAssistantText(objs), "last\nline2");
  assert.equal(lastAssistantText([]), "");
  assert.equal(lastAssistantText([{ type: "assistant", message: { content: "plain string" } }]), "plain string");
});

test("completeBody passes LOOM_PLUGIN_RUN_RETAIN through only when it names a mode", () => {
  const scope = { org: "o", namespace: "n" };
  const none = completeBody(scope, { session_id: "s1", reason: "clear" }, "", {});
  assert.equal("retain" in none, false);
  const bad = completeBody(scope, { session_id: "s1", reason: "clear" }, "", { LOOM_PLUGIN_RUN_RETAIN: "delete" });
  assert.equal("retain" in bad, false);
  const ok = completeBody(scope, { session_id: "s1", reason: "clear" }, "", { LOOM_PLUGIN_RUN_RETAIN: " Expire " });
  assert.equal(ok.retain, "expire");
  assert.equal(retainFor({ LOOM_PLUGIN_RUN_RETAIN: "demote" }), "demote");
});

test("run.complete carries what the session's tools SERVED as recalled_keys, never as memories_used", () => {
  const c = completeBody(scope, { session_id: "s1", reason: "exit" }, "Espresso.", {}, [
    "coffee_type",
    "coffee_type",
    " ",
    "where_i_live",
  ]);
  assert.deepEqual(c.recalled_keys, ["coffee_type", "where_i_live"]);
  assert.equal(c.memories_used, undefined, "reliance stays the agent's own label");
  assert.equal(
    completeBody(scope, { session_id: "s1", reason: "exit" }, "x").recalled_keys,
    undefined,
    "nothing served: no field",
  );
});

test("servedMemoryKeys reads a loom_search payload, a memory.get result, or a bare list; chunks are not memories", () => {
  const search = {
    structuredContent: {
      memory: {
        results: [
          { memory_key: "coffee_type", source: "fact" },
          { memory_key: "chunk:df59:0", source: "chunk" },
          { memory_key: "coffee_type" },
        ],
      },
    },
  };
  assert.deepEqual(servedMemoryKeys(search), ["coffee_type"]);
  assert.deepEqual(servedMemoryKeys({ found: true, memory_key: "where_i_live", value: "xyz" }), ["where_i_live"]);
  assert.deepEqual(servedMemoryKeys({ found: false }), []);
  assert.deepEqual(servedMemoryKeys({ results: [{ memory_key: "a" }, { memory_key: "b" }] }), ["a", "b"]);
  assert.deepEqual(servedMemoryKeys(null), []);
});

test("the recalled stash accumulates per session and reads back deduped", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-recalled-"));
  assert.deepEqual(readRecalledStash("sess-1", tmp), []);
  appendRecalledStash("sess-1", ["coffee_type"], tmp);
  appendRecalledStash("sess-1", ["where_i_live", "coffee_type"], tmp);
  assert.deepEqual(readRecalledStash("sess-1", tmp), ["coffee_type", "where_i_live"]);
  assert.deepEqual(readRecalledStash("sess-2", tmp), [], "another session sees nothing");
});

test("run.begin and run.complete bodies carry no secret the session typed", () => {
  const secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8";
  const begin = JSON.stringify(beginBody(scope, { session_id: "s1" }, `deploy with ${secret}`));
  const done = JSON.stringify(completeBody(scope, { session_id: "s1", reason: "exit" }, `used ${secret}`));
  for (const body of [begin, done]) {
    assert.ok(!body.includes(secret.slice(4)), body);
    assert.ok(body.includes("[redacted]"), body);
  }
});
