import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildObservation, fallbackReason, shouldObserve } from "../hooks/loom-capture.mjs";

const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: ["project:loom"] };

test("an observation carries the server's canonical fields, bounded", () => {
  const body = buildObservation(
    "PostToolUse",
    {
      session_id: "s1",
      tool_name: "Bash",
      tool_input: { command: "pytest -q tests/unit/test_observe.py" },
      tool_response: { stdout: "12 passed", stderr: "" },
    },
    scope,
  );
  assert.equal(body.tool_name, "Bash");
  assert.match(String(body.tool_input), /pytest -q/);
  assert.match(String(body.tool_output), /12 passed/);
  assert.equal(body.error, null);
  for (const k of ["files", "is_error", "content"]) assert.equal(k in body, false);
});

test("a failed tool call carries its error text", () => {
  const body = buildObservation(
    "PostToolUseFailure",
    {
      session_id: "s1",
      tool_name: "Edit",
      tool_input: { file_path: "server/main.py" },
      tool_response: { error: "old_string not found" },
    },
    scope,
  );
  assert.equal(body.error, "old_string not found");
  assert.ok(body.file_paths.includes("server/main.py"));
});

test("outputs are bounded", () => {
  const body = buildObservation(
    "PostToolUse",
    { session_id: "s1", tool_name: "Read", tool_input: { file_path: "a.py" }, tool_response: "x".repeat(50000) },
    scope,
  );
  assert.ok(String(body.tool_output).length <= 900);
});

test("a call to loom's own tools or a meta tool is not observed", () => {
  for (const t of [
    "mcp__loom__loom_memory_set",
    "mcp__loom__loom_memory_delete",
    "loom_knowledge_add",
    "ToolSearch",
    "AskUserQuestion",
    "todowrite",
  ]) {
    assert.equal(shouldObserve(t, ""), false, t);
  }
  for (const t of ["Bash", "Edit", "Read", "mcp__github__create_pr", "WebFetch", ""]) {
    assert.equal(shouldObserve(t, ""), true, t);
  }
  // the override replaces the list
  assert.equal(shouldObserve("Bash", "Bash,mcp__x__*"), false);
  assert.equal(shouldObserve("mcp__loom__loom_memory_set", "Bash"), true);
});

test("readSpanOf: a Read's offset/limit becomes the opened_file span", async () => {
  process.env.LOOM_HOOK_NO_MAIN = "1";
  const { readSpanOf } = await import("../hooks/loom-capture.mjs");
  const count = () => 500;
  assert.equal(readSpanOf({ file_path: "/r/src/a.cpp" }, count), "", "whole-file read: no span");
  assert.equal(readSpanOf({ file_path: "/r/src/a.cpp", offset: 100, limit: 40 }, count), "100-139");
  assert.equal(
    readSpanOf({ file_path: "/r/src/a.cpp", offset: 480, limit: 40 }, count),
    "480-500",
    "clipped to the file",
  );
  assert.equal(readSpanOf({ file_path: "/r/src/a.cpp", offset: 100 }, count), "100-500", "default limit, clipped");
  assert.equal(readSpanOf({ file_path: "/r/src/a.cpp", limit: 30 }, count), "1-30");
  assert.equal(
    readSpanOf({ file_path: "/r/gone.cpp", offset: 100, limit: 40 }, () => 0),
    "100-139",
    "uncountable file: from the input alone",
  );
  assert.equal(readSpanOf({ file_path: "/r/src/a.cpp", offset: 600, limit: 40 }, count), "", "past the end");
});

test("fallbackReason: a grep after a code answer names why, from the server's set", () => {
  assert.equal(fallbackReason({ traceId: "t", paths: [], abstained: true }), "abstained");
  assert.equal(fallbackReason({ traceId: "t", paths: [], abstained: false }), "empty");
  assert.equal(fallbackReason({ traceId: "t", paths: ["src/a.cpp"], abstained: false }), "preference");
  assert.equal(fallbackReason(undefined), "unattributed");
  // the server's vocabulary is the contract; every value here must be in it
  const serverSet = new Set([
    "abstained",
    "empty",
    "too_slow",
    "wrong_shape",
    "not_indexed",
    "stale",
    "preference",
    "unattributed",
  ]);
  for (const row of [{ abstained: true }, { paths: [] }, { paths: ["x"] }, undefined]) {
    assert.ok(serverSet.has(fallbackReason(row)));
  }
});

test("captured tool input, output and error are redacted before they are sent", () => {
  // Assembled at run time so the repo's own secret scanner does not flag the fixture.
  const pem = (edge) => `-----${edge} OPENSSH PRIVATE` + " KEY-----";
  const awsId = "AKIA" + "IOSFODNN7EXAMPLE";
  const key = [pem("BEGIN"), "b3BlbnNzaC1rZXktdjEAAAAA", pem("END")];
  const body = buildObservation(
    "PostToolUseFailure",
    {
      session_id: "s1",
      tool_name: "Bash",
      tool_input: {
        command: "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 curl -H 'Authorization: Bearer sk-live-abc' x",
      },
      tool_response: {
        stdout: `${key.join("\n")}\n${awsId}\npassword = hunter2\nxoxb-1234567890-abcdefghij`,
        error: "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln rejected; api_key: 's3cr3t'",
      },
    },
    scope,
  );
  const sent = [body.tool_input, body.tool_output, body.error].join("\n");
  for (const secret of [
    "ghp_abcdefghij",
    "sk-live-abc",
    "b3BlbnNzaC1rZXkt",
    awsId,
    "hunter2",
    "xoxb-1234567890",
    "eyJhbGciOiJIUzI1NiJ9",
    "s3cr3t",
  ]) {
    assert.equal(sent.includes(secret), false, `${secret} leaked`);
  }
  assert.match(body.tool_input, /curl -H/, "the rest of the command survives");
  assert.deepEqual(Object.keys(JSON.parse(body.tool_input)), ["command"], "redacted JSON still parses");
});

test("redaction leaves ordinary text alone", () => {
  const body = buildObservation(
    "PostToolUse",
    { session_id: "s1", tool_name: "Bash", tool_input: { command: "echo max_tokens=800" }, tool_response: "ok" },
    scope,
  );
  assert.match(body.tool_input, /max_tokens=800/);
});
