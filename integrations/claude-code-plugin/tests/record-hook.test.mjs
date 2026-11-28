// The recording launcher: the host waits for a hand-off, not for a request.
// Each case runs hooks/loom-record.mjs as the host does against a fake Loom
// that keeps every answer back until the test lets it go, so "the hook
// returned" and "the record arrived" are observed apart.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hostEnv, withTmpdir } from "./host-env.mjs";
import { removeSandbox } from "./hook-sandbox.mjs";
import { RECORDERS } from "../hooks/loom-record.mjs";
import { writeRunBegun } from "../lib/run-hook.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK = path.join(root, "hooks", "loom-record.mjs");
const SESSION = "record-session-1";
const TOKEN = [
  Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url"),
  Buffer.from(JSON.stringify({ org: "acme", namespaces: ["agent-test"], sub: "tester" })).toString("base64url"),
  "signature",
].join(".");

/** A fake Loom that records each op as it arrives and answers none of them
 *  until `release()`; `close()` drops whatever is still held. */
async function heldLoom() {
  const ops = [];
  const held = [];
  const sockets = new Set();
  let open = false;
  const answer = (res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ rendered: "", results: [], repos: [] }));
  };
  const server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      ops.push(String(req.url).replace("/v1/", ""));
      if (open) answer(res);
      else held.push(res);
    });
  });
  server.on("connection", (s) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    ops,
    release() {
      open = true;
      for (const res of held.splice(0)) answer(res);
    },
    close: () =>
      new Promise((r) => {
        for (const s of sockets) s.destroy();
        server.close(() => r());
      }),
  };
}

function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-record-hook-"));
  const box = { dir, home: path.join(dir, "home"), tmp: path.join(dir, "tmp"), cwd: path.join(dir, "work") };
  for (const d of [box.home, box.tmp, box.cwd]) fs.mkdirSync(d);
  execFileSync("git", ["init", "-q"], { cwd: box.cwd });
  box.transcript = path.join(dir, "session.jsonl");
  const lines = [
    { type: "user", uuid: "u1", message: { role: "user", content: "fix the flaky parser test please" } },
    {
      type: "assistant",
      uuid: "a1",
      message: {
        id: "m1",
        model: "claude-test",
        role: "assistant",
        content: [{ type: "text", text: "Fixed: the parser test no longer depends on wall-clock time." }],
        usage: { input_tokens: 10, output_tokens: 5 },
      },
    },
  ];
  fs.writeFileSync(box.transcript, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  return box;
}

function env(box, url) {
  return hostEnv({
    PATH: process.env.PATH,
    HOME: box.home,
    TMPDIR: box.tmp,
    LOOM_BASE_URL: url,
    LOOM_API_KEY: TOKEN,
    LOOM_AUTO_SIGNUP: "off",
    // Longer than loom-run's round trip to a local fake: the conversation
    // hook is still waiting when the run closes.
    LOOM_CONV_SETTLE_MS: "300",
  });
}

function payload(event, box) {
  return JSON.stringify({
    hook_event_name: event,
    session_id: SESSION,
    transcript_path: box.transcript,
    cwd: box.cwd,
    tool_name: "Bash",
    tool_input: { command: "npm test" },
    tool_response: { stdout: "ok" },
    reason: "exit",
  });
}

/** Run the launcher as the host does; resolves {code, stdout, stderr} when it exits. */
function runHook(event, box, url) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, event], { env: env(box, url), cwd: os.tmpdir() });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    child.stdin.end(payload(event, box));
  });
}

async function arrived(loom, wanted, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !wanted.every((op) => loom.ops.includes(op))) {
    await new Promise((r) => setTimeout(r, 20));
  }
  assert.deepEqual(
    wanted.filter((op) => !loom.ops.includes(op)),
    [],
    `ops that never arrived; seen: ${loom.ops.join(", ")}`,
  );
}

test("the launcher records with scripts that exist", () => {
  for (const names of Object.values(RECORDERS)) {
    for (const name of names) assert.ok(fs.existsSync(path.join(root, "hooks", `${name}.mjs`)), name);
  }
});

test("a tool call's hook returns while the server has answered nothing, and the observation still arrives", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  try {
    const r = await runHook("PostToolUse", box, loom.url);
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    await arrived(loom, ["observe.record"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("one Stop hand-off carries every recording script's write", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  try {
    const r = await runHook("Stop", box, loom.url);
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    await arrived(loom, ["observe.crystallize", "cost.record_turns", "working_memory.set", "conversation.append"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("at SessionEnd the run's close does not end the scripts still at work beside it", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  loom.release(); // run.complete is awaited: answer at once
  try {
    withTmpdir(box.tmp, () => writeRunBegun(SESSION, `cc-${SESSION}`));
    const r = await runHook("SessionEnd", box, loom.url);
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    // The conversation hook reads the transcript only after its settle, which
    // outlasts the run's close.
    await arrived(loom, ["observe.crystallize", "run.complete", "memory.set_from_messages", "conversation.append"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("the hand-off outlives the process group that ran the hook", { skip: process.platform === "win32" }, async () => {
  const box = sandbox();
  const loom = await heldLoom();
  // A stand-in host in its own process group: it runs the hook, says so, and stays.
  const host = spawn(
    process.execPath,
    [
      "-e",
      `const { spawn } = require("node:child_process");
       const hook = spawn(process.execPath, [process.argv[1], "PostToolUse"], { stdio: ["pipe", "ignore", "ignore"] });
       hook.on("close", () => process.stdout.write("returned"));
       hook.stdin.end(process.argv[2]);
       setInterval(() => {}, 1000);`,
      HOOK,
      payload("PostToolUse", box),
    ],
    { env: env(box, loom.url), cwd: os.tmpdir(), detached: true, stdio: ["ignore", "pipe", "ignore"] },
  );
  try {
    await new Promise((resolve) => host.stdout.once("data", resolve));
    const gone = new Promise((resolve) => host.once("close", resolve));
    process.kill(-host.pid, "SIGKILL");
    await gone;
    await arrived(loom, ["observe.record"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});
