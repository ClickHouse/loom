// The sensory filter on capture and the first-prompt recall mode.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

process.env.LOOM_HOOK_NO_MAIN = "1";
const { buildObservation, dedupeKey, decideDedupe, DEDUPE_WINDOW_S } = await import("../hooks/loom-capture.mjs");
const { firstPromptDone, markFirstPromptDone } = await import("../hooks/loom-prompt-recall.mjs");

const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "p", tags: ["project:p"] };

function obs(tool, input, event = "PostToolUse") {
  return buildObservation(event, { tool_name: tool, tool_input: input, session_id: "s" }, scope);
}

test("dedupeKey: only read-shaped, successful observations have one", () => {
  assert.equal(dedupeKey(obs("Read", { file_path: "/r/a.py" })), "Read:/r/a.py");
  assert.equal(dedupeKey(obs("Grep", { pattern: "foo", path: "/r/src" })), "Grep:/r/src");
  assert.equal(dedupeKey(obs("Bash", { command: "cat a.py" })), ""); // a command run, not a read
  assert.equal(dedupeKey(obs("Edit", { file_path: "/r/a.py" })), ""); // edits always post
  assert.equal(dedupeKey(obs("Read", { file_path: "/r/a.py" }, "PostToolUseFailure")), ""); // errors always post
  assert.equal(dedupeKey(obs("Read", {})), ""); // nothing named: nothing to dedupe on
  assert.equal(dedupeKey(null), "");
});

test("decideDedupe: a repeat inside the window is suppressed, outside it posts and re-arms", () => {
  const t0 = 1_000_000;
  let r = decideDedupe({}, "Read:/r/a.py", t0, 60);
  assert.equal(r.suppress, false);
  r = decideDedupe(r.state, "Read:/r/a.py", t0 + 30_000, 60);
  assert.equal(r.suppress, true);
  r = decideDedupe(r.state, "Read:/r/b.py", t0 + 30_000, 60); // another file: posts
  assert.equal(r.suppress, false);
  r = decideDedupe(r.state, "Read:/r/a.py", t0 + 61_000, 60); // window passed: posts, re-arms
  assert.equal(r.suppress, false);
  assert.equal(r.state["Read:/r/a.py"], t0 + 61_000);
  // window 0 disables; an empty key never suppresses
  assert.equal(decideDedupe(r.state, "Read:/r/a.py", t0 + 62_000, 0).suppress, false);
  assert.equal(decideDedupe(r.state, "", t0 + 62_000, 60).suppress, false);
  assert.equal(DEDUPE_WINDOW_S, 60);
});

test("decideDedupe: the state is bounded to 200 keys, oldest dropped", () => {
  let state = {};
  for (let i = 0; i < 250; i++) state = decideDedupe(state, `Read:/r/f${i}.py`, 1000 + i, 60).state;
  assert.equal(Object.keys(state).length, 200);
  assert.equal("Read:/r/f0.py" in state, false);
  assert.equal("Read:/r/f249.py" in state, true);
});

test("first-prompt recall: the marker is per session and one-shot", () => {
  const sid = `test-${process.pid}-${Date.now()}`;
  assert.equal(firstPromptDone(sid), false);
  markFirstPromptDone(sid);
  assert.equal(firstPromptDone(sid), true);
  assert.equal(firstPromptDone(sid + "-other"), false);
  // the marker lives in tmpdir under a hashed name, never the raw session id
  const names = fs.readdirSync(os.tmpdir()).filter((n) => n.startsWith("loom-prompt-recall-first-"));
  assert.ok(names.length >= 1 && !names.some((n) => n.includes(sid)));
  for (const n of names)
    if (fs.statSync(path.join(os.tmpdir(), n)).mtimeMs > Date.now() - 5000) fs.unlinkSync(path.join(os.tmpdir(), n));
});
