// The host payload and the environment, read at the boundary.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { envNumber, envSwitch, normalizePayload } from "../lib/loom-client.mjs";

test("a Claude Code payload passes through unchanged", () => {
  const p = {
    hook_event_name: "PostToolUse",
    session_id: "s1",
    transcript_path: "/t.jsonl",
    cwd: "/w",
    tool_name: "Read",
    tool_input: { file_path: "/w/a" },
    tool_response: { content: "x" },
    extra: { kept: true },
  };
  assert.deepEqual(normalizePayload(p), p);
});

test("the spellings hooks.generic.json promises fold onto the canonical names", () => {
  const p = normalizePayload({
    sessionId: "s2",
    transcriptPath: "/t",
    user_prompt: "hi",
    toolName: "shell",
    toolInput: { command: "ls" },
    tool_result: "out",
  });
  assert.equal(p.session_id, "s2");
  assert.equal(p.transcript_path, "/t");
  assert.equal(p.prompt, "hi");
  assert.equal(p.tool_name, "shell");
  assert.deepEqual(p.tool_input, { command: "ls" });
  assert.equal(p.tool_response, "out");
  assert.equal(normalizePayload({ tool: "Grep", input: { pattern: "x" }, output: "o" }).tool_name, "Grep");
});

test("wrong-typed fields are dropped or stringified, never passed on", () => {
  const p = normalizePayload({ session_id: 42, cwd: 3, transcript_path: null, prompt: {}, tool_name: ["x"] });
  assert.equal(p.session_id, "42");
  assert.equal(p.cwd, "3");
  assert.equal("transcript_path" in p, false);
  assert.equal("prompt" in p, false);
  assert.equal("tool_name" in p, false);
  assert.deepEqual(normalizePayload([1, 2]), {});
  assert.deepEqual(normalizePayload(null), {});
});

test("envSwitch reads on/off words in any case and falls back otherwise", () => {
  for (const v of ["on", "TRUE", "1", "yes"]) assert.equal(envSwitch("X", false, { X: v }), true, v);
  for (const v of ["off", "False", "0", "no"]) assert.equal(envSwitch("X", true, { X: v }), false, v);
  assert.equal(envSwitch("X", true, {}), true);
  assert.equal(envSwitch("X", false, { X: "maybe" }), false);
});

test("envNumber clamps, and an unset, empty or non-numeric value is the fallback", () => {
  assert.equal(envNumber("N", 3, { env: { N: "9" }, max: 5 }), 5);
  assert.equal(envNumber("N", 3, { env: { N: "-2" }, min: 1 }), 1);
  assert.equal(envNumber("N", 3, { env: { N: "abc" } }), 3);
  assert.equal(envNumber("N", 3, { env: { N: "" } }), 3);
  assert.equal(envNumber("N", 3, { env: {} }), 3);
  assert.equal(envNumber("N", 350, { env: { N: "0" }, min: 0 }), 0);
});

test("diagnostics never carry a bearer token or a JWT", async () => {
  const { redactSecrets } = await import("../lib/loom-client.mjs");
  const jwt = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0In0.c2ln";
  assert.equal(redactSecrets(`401 for Bearer abc.def.ghi at x`, ""), "401 for Bearer [redacted] at x");
  assert.equal(redactSecrets(`token ${jwt} rejected`, ""), "token [redacted] rejected");
  assert.equal(redactSecrets("opaque-secret-value leaked", "opaque-secret-value"), "[redacted] leaked");
  assert.equal(redactSecrets("nothing here", ""), "nothing here");
});

test("diagnostics never carry Anthropic, OpenAI or GitLab keys, basic auth or URL passwords", async () => {
  const { redactSecrets } = await import("../lib/loom-client.mjs");
  // Assembled at run time so the repo's own secret scanner does not flag the fixture.
  const tail = "abcdefghijklmnopqrstuvwxyz0123";
  for (const prefix of ["sk-" + "ant-api03-", "sk-" + "proj-", "glpat" + "-"]) {
    assert.equal(redactSecrets(`key ${prefix}${tail} rejected`, ""), "key [redacted] rejected");
  }
  assert.equal(redactSecrets("Authorization: Basic dXNlcjpwYXNz sent", ""), "Authorization: Basic [redacted] sent");
  assert.equal(redactSecrets('{"authorization": "Basic dXNlcjpwYXNz"}', ""), '{"authorization": "Basic [redacted]"}');
  assert.equal(
    redactSecrets("clone https://alice:hunter2@git.example.com/r.git failed", ""),
    "clone https://[redacted]@git.example.com/r.git failed",
  );
  assert.equal(
    redactSecrets("Basic setup at http://localhost:7777/v1 ok", ""),
    "Basic setup at http://localhost:7777/v1 ok",
  );
});

test("redaction is linear: an 80 KB adversarial output takes well under 200 ms per shape", async () => {
  const { redactSecrets } = await import("../lib/loom-client.mjs");
  const n = 80_000;
  const shapes = {
    url: "a://" + "x:".repeat(n / 2),
    dotted: "a.".repeat(n / 2),
    urlDots: "https://" + "a.b:".repeat(n / 4),
    jwt: "eyJ-".repeat(n / 4),
  };
  for (const [name, text] of Object.entries(shapes)) {
    const started = performance.now();
    redactSecrets(text, "");
    const ms = performance.now() - started;
    assert.ok(ms < 200, `${name} took ${Math.round(ms)} ms`);
  }
});

test("redactHead scans only the head its caller keeps, and a secret on the cut is still matched", async () => {
  const { redactHead } = await import("../lib/loom-client.mjs");
  const secret = "ghp_" + "a".repeat(36);
  const text = "x".repeat(94) + " " + secret + " " + "y".repeat(200_000);
  const head = redactHead(text, 100, "");
  assert.ok(head.length < 100 + 17 * 1024);
  assert.ok(!head.slice(0, 100).includes("ghp_"));
  assert.ok(head.startsWith("x".repeat(94) + " [redacted] y"));
});

test("redactHead never keeps a token split by its cut, even after an earlier secret shrinks", async () => {
  const { redactHead } = await import("../lib/loom-client.mjs");
  const key = "-----BEGIN " + "PRIVATE KEY-----\n" + "A".repeat(20_000) + "\n-----END PRIVATE KEY-----";
  const max = 8000;
  // At max + 16 KiB - 16, 16 of the token's 36 characters sit before the old cut.
  const tokenAt = (at) => {
    const filler = "word ".repeat(Math.ceil(at / 5)).slice(0, at - key.length - 2) + " ";
    return key + " " + filler + "ghp_Z9y8X7w6V5u4T3s2" + "q".repeat(20) + " tail".repeat(20_000);
  };
  for (let at = max + 16 * 1024 - 40; at < 90_000; at += at < max + 16 * 1024 ? 1 : 97) {
    const head = redactHead(tokenAt(at), max, "").slice(0, max);
    assert.ok(head.startsWith("[redacted private key] word"), `token at ${at}`);
    assert.ok(!head.includes("ghp_") && !head.includes("Z9y8"), `token at ${at}`);
  }
});

/** readStdinJson in a child process fed `input`; resolves the object it read. */
async function readStdinIn(input) {
  const { spawn } = await import("node:child_process");
  const client = new URL("../lib/loom-client.mjs", import.meta.url).href;
  const src = `import(${JSON.stringify(client)}).then((m) => m.readStdinJson()).then((p) => process.stdout.write(JSON.stringify(p)))`;
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "-e", src], { stdio: ["pipe", "pipe", "inherit"] });
    let out = "";
    child.stdout.on("data", (c) => (out += c));
    child.on("error", reject);
    child.on("close", () => resolve(JSON.parse(out || "null")));
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

test("readStdinJson reads a large payload whole, and drops one over its cap as {}", async () => {
  const under = await readStdinIn(JSON.stringify({ session_id: "s1", pad: "x".repeat(2 * 1024 * 1024) }));
  assert.equal(under.session_id, "s1");
  assert.equal(under.pad.length, 2 * 1024 * 1024);
  const over = await readStdinIn(JSON.stringify({ session_id: "s1", pad: "x".repeat(33 * 1024 * 1024) }));
  assert.deepEqual(over, {});
  assert.deepEqual(await readStdinIn("{not json"), {});
  assert.deepEqual(await readStdinIn(""), {});
});
