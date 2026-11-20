import "./temp-home.mjs";
import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { promptHeaders } from "../lib/prompt-context.mjs";

test("host prompt IDs bind hook calls without capturing prompt text", () => {
  const claude = promptHeaders({ session_id: "conversation-a", prompt_id: "prompt-a", prompt: "private text" });
  const codex = promptHeaders({ session_id: "conversation-a", turn_id: "prompt-a" });
  assert.equal(claude["x-loom-prompt-id"], "prompt-a");
  assert.equal(codex["x-loom-prompt-id"], "prompt-a");
  assert.equal(claude["x-loom-conversation-id"], "conversation-a");
  assert.equal(claude["x-loom-call-origin"], "hook");
  assert.equal(JSON.stringify(claude).includes("private text"), false);
});

test("optional IDs are independent and ambiguous prompt IDs are dropped", () => {
  assert.equal(promptHeaders({ session_id: "conversation-a" })["x-loom-conversation-id"], "conversation-a");
  assert.equal(promptHeaders({ prompt_id: "prompt-a" })["x-loom-prompt-id"], "prompt-a");
  assert.equal(promptHeaders({ session_id: "c", prompt_id: "a", turn_id: "b" })["x-loom-prompt-id"], undefined);
  assert.equal(promptHeaders({ session_id: "c", prompt_id: "bad\r\nheader" })["x-loom-prompt-id"], undefined);
  assert.equal(promptHeaders({ session_id: "c", turn_id: "x".repeat(129) })["x-loom-prompt-id"], undefined);
});

test("identifiers reject trailing line breaks without losing hook origin", () => {
  for (const suffix of ["\n", "\r", "\r\n"]) {
    const badPrompt = promptHeaders({ session_id: "c", turn_id: `p${suffix}` });
    const badSession = promptHeaders({ session_id: `c${suffix}`, turn_id: "p" });
    assert.equal(badPrompt["x-loom-prompt-id"], undefined);
    assert.equal(badSession["x-loom-conversation-id"], undefined);
    assert.equal(badPrompt["x-loom-call-origin"], "hook");
    const headers = promptHeaders({ session_id: "c", turn_id: "p", tool_use_id: `call${suffix}` });
    assert.equal(headers["x-loom-tool-call-id"], undefined);
  }
});

test("simultaneous turns and tool IDs stay separate", () => {
  const a = promptHeaders({ session_id: "c", turn_id: "a", tool_use_id: "call-a", hook_event_name: "PreToolUse" });
  const b = promptHeaders({ session_id: "c", turn_id: "b", tool_use_id: "call-b", hook_event_name: "PostToolUse" });
  assert.equal(a["x-loom-prompt-id"], "a");
  assert.equal(b["x-loom-prompt-id"], "b");
  assert.equal(a["x-loom-tool-call-id"], "call-a");
  assert.equal(b["x-loom-tool-call-id"], "call-b");
  assert.equal(b["x-loom-hook-event"], "PostToolUse");
});

test("the shared HTTP transport carries only the event's explicit prompt context", () => {
  const url = new URL("../lib/loom-client.mjs", import.meta.url).href;
  const script = `
    const { readStdinJson, requestJson } = await import(${JSON.stringify(url)});
    await readStdinJson();
    await requestJson("http://127.0.0.1:1/v1/memory.get", {
      body: { memory_key: "example" },
      fetchImpl: async (url, options) => {
        console.log(JSON.stringify({ headers: options.headers, body: options.body }));
        return { status: 200, json: async () => ({}) };
      }
    });
  `;
  const env = {
    ...process.env,
    LOOM_CONFIG_FILE: "/nonexistent/loom-config.json",
    LOOM_API_KEY_FILE: "/nonexistent/loom-token",
    LOOM_API_KEY: "test-secret-must-not-leak",
    CLAUDE_PLUGIN_OPTION_TOKEN: "",
  };
  const capture = (payload) => {
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: 5000,
      env,
    });
    assert.equal(child.status, 0, child.stderr);
    return JSON.parse(child.stdout);
  };
  const a = capture({ session_id: "c", turn_id: "a", hook_event_name: "UserPromptSubmit", prompt: "private prompt" });
  const b = capture({ session_id: "c", turn_id: "b" });
  const absent = capture({ session_id: "c" });
  const secret = capture({ session_id: "c", turn_id: env.LOOM_API_KEY });
  assert.equal(a.headers["x-loom-prompt-id"], "a");
  assert.equal(b.headers["x-loom-prompt-id"], "b");
  assert.equal(absent.headers["x-loom-prompt-id"], undefined);
  assert.equal(secret.headers["x-loom-prompt-id"], undefined);
  assert.deepEqual(JSON.parse(a.body), { memory_key: "example" });
  assert.equal(JSON.stringify(a).includes("private prompt"), false);
});

test("invocation origin survives a hook event without a host prompt", () => {
  const headers = promptHeaders({ session_id: "session-a", hook_event_name: "SessionStart" });
  assert.equal(headers["x-loom-call-origin"], "hook");
  assert.equal(headers["x-loom-hook-event"], "SessionStart");
  assert.equal(headers["x-loom-prompt-id"], undefined);
  assert.equal(headers["x-loom-conversation-id"], "session-a");
});

test("detached hooks declare their real entrypoint without inheriting a prompt", async () => {
  const { entrypointHeaders } = await import("../lib/prompt-context.mjs");
  const headers = entrypointHeaders("/tmp/plugin/hooks/loom-flush.mjs", "/tmp/plugin/hooks");
  assert.equal(headers["x-loom-call-origin"], "hook");
  assert.equal(headers["x-loom-hook-name"], "loom-flush");
  assert.equal(headers["x-loom-prompt-id"], undefined);
  assert.deepEqual(entrypointHeaders("/tmp/other/loom-flush.mjs", "/tmp/plugin/hooks"), {});
});
