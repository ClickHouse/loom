// The launcher: at a prompt, after a tool call and at Stop the host waits for
// a hand-off, not for a recording request; at a prompt it also waits for the
// two lookups, in the same process; at SessionEnd it waits for the records.
// Each case runs hooks/loom-record.mjs as the host does against a fake Loom
// that keeps its answers back until the test lets them go, so "the hook
// returned" and "the record arrived" are observed apart.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { assertMode, hostEnv, withTmpdir } from "./host-env.mjs";
import { removeSandbox } from "./hook-sandbox.mjs";
import { LOOKUPS, RECORDERS, mergedContext } from "../hooks/loom-record.mjs";
import { cachePathFor } from "../lib/repo-scope.mjs";
import { readRunState, writeRunBegun } from "../lib/run-hook.mjs";
import { sessionStatePath, writeJsonFile } from "../lib/tmp-state.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const HOOK = path.join(root, "hooks", "loom-record.mjs");
const HEALTH = path.join(root, "hooks", "loom-namespace-health.mjs");
const SESSION = "record-session-1";
const TOKEN = [
  Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url"),
  Buffer.from(JSON.stringify({ org: "acme", namespaces: ["agent-test"], sub: "tester" })).toString("base64url"),
  "signature",
].join(".");

/** A fake Loom that records each op as it arrives and answers none of them
 *  until `release()`, except the ops in `now`; `replies` is what an op
 *  answers when it has more to say than an empty result. `close()` drops
 *  whatever is still held. */
async function heldLoom({ replies = {}, now = [] } = {}) {
  const ops = [];
  const held = [];
  const sockets = new Set();
  let open = false;
  const answer = (res, op) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify(replies[op] || { rendered: "", results: [], repos: [] }));
  };
  const server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      const op = String(req.url).replace("/v1/", "");
      ops.push(op);
      if (open || now.includes(op)) answer(res, op);
      else held.push([res, op]);
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
      for (const [res, op] of held.splice(0)) answer(res, op);
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
      uuid: "a0",
      message: {
        id: "m0",
        model: "claude-test",
        role: "assistant",
        content: [{ type: "tool_use", id: "t0", name: "Edit", input: { file_path: path.join(box.cwd, "parser.py") } }],
        usage: { input_tokens: 10, output_tokens: 5 },
      },
    },
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

function env(box, url, extra = {}) {
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
    // What a script throws in the launcher then reaches stderr, which the cases check.
    LOOM_HOOK_DEBUG: "on",
    ...extra,
  });
}

/** Environment in which a detached copy of the launcher ends before it runs
 *  anything, as one killed at its start: whatever was handed to it stays. */
function dyingCopy(box) {
  const preload = path.join(box.dir, "die-as-copy.cjs");
  fs.writeFileSync(preload, "if (process.argv[4] && /loom-record\\.mjs$/.test(process.argv[1])) process.exit(1);\n");
  return { NODE_OPTIONS: `--require ${preload}` };
}

/** Environment in which a detached copy of the launcher starts only once
 *  `go()` is called: a copy the session's later hooks get ahead of. */
function heldCopy(box) {
  const preload = path.join(box.dir, "hold-copy.cjs");
  const gate = path.join(box.dir, "go");
  fs.writeFileSync(
    preload,
    `if (process.argv[4] && /loom-record\\.mjs$/.test(process.argv[1])) {
       const wait = new Int32Array(new SharedArrayBuffer(4));
       while (!require("node:fs").existsSync(${JSON.stringify(gate)})) Atomics.wait(wait, 0, 0, 20);
     }\n`,
  );
  return { env: { NODE_OPTIONS: `--require ${preload}` }, go: () => fs.writeFileSync(gate, "") };
}

/** The payloads handed off and not yet recorded. */
function handedOff(box) {
  return fs.readdirSync(box.tmp).filter((name) => name.startsWith("loom-record-"));
}

function payload(event, box, extra = {}) {
  return JSON.stringify({
    hook_event_name: event,
    session_id: SESSION,
    transcript_path: box.transcript,
    cwd: box.cwd,
    tool_name: "Bash",
    tool_input: { command: "npm test" },
    tool_response: { stdout: "ok" },
    reason: "exit",
    ...extra,
  });
}

/** Run the launcher (or `script`) with `args`; resolves {code, stdout,
 *  stderr} when it exits. `leaveOpen` is a host that writes the payload and
 *  never closes the hook's stdin. */
function runLauncher(args, hookEnv, stdin = "", { leaveOpen = false, script = HOOK } = {}) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [script, ...args], { env: hookEnv, cwd: os.tmpdir() });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    if (leaveOpen) child.stdin.write(stdin);
    else child.stdin.end(stdin);
  });
}

/** Run the launcher as the host does. */
function runHook(event, box, url, extra) {
  return runLauncher([event], env(box, url, extra), payload(event, box));
}

async function until(pred, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && !pred()) await new Promise((r) => setTimeout(r, 20));
  return pred();
}

async function arrived(loom, wanted, timeoutMs = 20000) {
  await until(() => wanted.every((op) => loom.ops.includes(op)), timeoutMs);
  assert.deepEqual(
    wanted.filter((op) => !loom.ops.includes(op)),
    [],
    `ops that never arrived; seen: ${loom.ops.join(", ")}`,
  );
}

/** Every handed-off payload has been recorded and its job file removed. */
function recorded(box) {
  return until(() => handedOff(box).length === 0);
}

test("the launcher runs scripts that exist", () => {
  for (const names of [...Object.values(RECORDERS), ...Object.values(LOOKUPS)]) {
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
    // The payload stays on disk only while the scripts are at work on it.
    assert.ok(await until(() => handedOff(box).length === 0), `left behind: ${handedOff(box)}`);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("with no credential the launcher hands nothing off", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  try {
    // A copy, had one been started, would die here and leave its job file.
    const r = await runHook("PostToolUse", box, loom.url, { ...dyingCopy(box), LOOM_API_KEY: "" });
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    assert.deepEqual(fs.readdirSync(box.tmp), []);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("a copy that dies before it records leaves its job file and no cursor, and the replay delivers it", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  loom.release();
  try {
    const r = await runHook("Stop", box, loom.url, dyingCopy(box));
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    // The job file and nothing else: no cursor was written, so the next Stop
    // starts from the same turns.
    const left = fs.readdirSync(box.tmp);
    assert.equal(left.length, 1, String(left));
    assert.match(left[0], /^loom-record-Stop-\d{13}-\d+\.json$/);
    const job = path.join(box.tmp, left[0]);
    assertMode(job, 0o600);
    assert.deepEqual(loom.ops, []);
    // The replay skills/loom-status/reference.md names for such a file.
    const replay = await runLauncher(["Stop", "-", job], env(box, loom.url));
    assert.deepEqual(replay, { code: 0, stdout: "", stderr: "" });
    await arrived(loom, ["observe.crystallize", "cost.record_turns", "working_memory.set", "conversation.append"]);
    assert.equal(loom.ops.filter((op) => op === "observe.crystallize").length, 1, "the dead copy sent nothing");
    assert.equal(fs.existsSync(job), false);
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

const SEARCH = {
  abstain: false,
  retrieval_confidence: 0.6,
  retrieval_risk: "low",
  results: [{ path: "src/Merge/Executor.h", start_line: 10, end_line: 40, symbol: "DB::Executor" }],
};
const PACK = {
  section_meta: { memories: { abstain: false, retrieval_confidence: 0.8 } },
  blocks: [
    { section: "memories", label: "recall.memories", rendered: "- use the unit suite", citations: ["memory:n/k"] },
  ],
};
const AUDIT = { namespace: "agent-test", findings: [{ code: "exact_key_store", severity: "critical", count: 12 }] };
const PROMPT = { prompt: "where are merges scheduled? Include our saved decision." };

/** Mark the sandbox's checkout as an indexed repository, as SessionStart's lookup does. */
function indexed(box) {
  const top = fs.realpathSync(box.cwd);
  fs.writeFileSync(
    cachePathFor(top, box.tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "acme", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
}

test("mergedContext: the lookups' context as one answer, in their order; none without any", () => {
  const answer = (text) => ({ hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext: text } });
  assert.deepEqual(
    mergedContext("UserPromptSubmit", [answer("code"), undefined, answer("memory")]),
    answer("code\nmemory"),
  );
  assert.deepEqual(mergedContext("UserPromptSubmit", [undefined, answer("memory")]), answer("memory"));
  assert.equal(mergedContext("UserPromptSubmit", [undefined, undefined]), null);
  assert.equal(mergedContext("UserPromptSubmit", []), null);
});

test("a prompt's hook prints what both lookups found and waits for neither the run's open nor the audit", async () => {
  const box = sandbox();
  indexed(box);
  const loom = await heldLoom({
    replies: { "code.search": SEARCH, "context.pack": PACK },
    now: ["code.search", "context.pack"],
  });
  try {
    const r = await runLauncher(["UserPromptSubmit"], env(box, loom.url), payload("UserPromptSubmit", box, PROMPT));
    assert.equal(r.code, 0);
    assert.equal(r.stderr, "");
    const out = JSON.parse(r.stdout).hookSpecificOutput;
    assert.equal(out.hookEventName, "UserPromptSubmit");
    // One answer: the code pointers, then the recalled memory.
    assert.match(out.additionalContext, /src\/Merge\/Executor\.h:10-40 DB::Executor[\s\S]*\n[\s\S]*use the unit suite/);
    // The hook is back and the server has answered neither recording call.
    await arrived(loom, ["run.begin", "namespace.audit"]);
    // A SessionEnd or a second prompt that comes now finds the begin to ask again.
    const state = withTmpdir(box.tmp, () => readRunState(SESSION));
    assert.equal(state.attempted, true);
    assert.equal(state.body.run_id, `cc-${SESSION}`);
    loom.release();
    assert.ok(await recorded(box), `left behind: ${handedOff(box)}`);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("a script the launcher runs beside others reports what it throws under LOOM_HOOK_DEBUG", async () => {
  const box = sandbox();
  const client = pathToFileURL(path.join(root, "lib", "loom-client.mjs")).href;
  const throwing = path.join(box.dir, "loom-throws.mjs");
  const driver = path.join(box.dir, "driver.mjs");
  fs.writeFileSync(
    throwing,
    `import { runHookMain } from ${JSON.stringify(client)};
     runHookMain(async () => { throw new Error("no index for Bearer ${TOKEN}\\nnot shown"); });\n`,
  );
  const urls = [throwing, path.join(box.dir, "loom-missing.mjs")].map((file) => pathToFileURL(file).href);
  fs.writeFileSync(
    driver,
    `import { runHooksTogether } from ${JSON.stringify(client)};
     await runHooksTogether("{}", ${JSON.stringify(urls)});\n`,
  );
  try {
    const r = await runLauncher([], env(box, "http://127.0.0.1:9"), "", { script: driver });
    assert.equal(r.code, 0);
    const lines = r.stderr.trim().split("\n");
    assert.equal(lines.length, 2, r.stderr);
    // The script that does not load, then the one that throws: each by name, one redacted line.
    assert.match(lines[0], /^loom hook loom-missing\.mjs: .*Cannot find module/);
    assert.match(lines[1], /^loom hook loom-throws\.mjs: Error: no index for /);
    assert.equal(r.stderr.includes(TOKEN), false, "the token is redacted");
    const quiet = await runLauncher([], env(box, "http://127.0.0.1:9", { LOOM_HOOK_DEBUG: "off" }), "", {
      script: driver,
    });
    assert.deepEqual(quiet, { code: 0, stdout: "", stderr: "" });
  } finally {
    await removeSandbox(box);
  }
});

test("a prompt with nothing to add prints nothing", async () => {
  const box = sandbox();
  indexed(box);
  const loom = await heldLoom();
  loom.release();
  try {
    const r = await runLauncher(["UserPromptSubmit"], env(box, loom.url), payload("UserPromptSubmit", box, PROMPT));
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    await arrived(loom, ["run.begin", "namespace.audit"]);
    assert.ok(await recorded(box), `left behind: ${handedOff(box)}`);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("a session that ends before the prompt's copy has started leaves no run open", async () => {
  const box = sandbox();
  const loom = await heldLoom({ replies: { "run.begin": { run_id: `cc-${SESSION}` } } });
  loom.release();
  const runOps = () => loom.ops.filter((op) => op.startsWith("run."));
  const copy = heldCopy(box);
  try {
    const hookEnv = env(box, loom.url, copy.env);
    const r = await runLauncher(["UserPromptSubmit"], hookEnv, payload("UserPromptSubmit", box, PROMPT));
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    // The hook is back and its copy has asked nothing: the begin is on disk all the same.
    assert.deepEqual(runOps(), []);
    assert.equal(withTmpdir(box.tmp, () => readRunState(SESSION)).body.goal, PROMPT.prompt);
    // The session ends first (an abort right after the prompt): its hook opens the run and closes it.
    assert.deepEqual(await runHook("SessionEnd", box, loom.url), { code: 0, stdout: "", stderr: "" });
    assert.deepEqual(runOps(), ["run.begin", "run.complete"]);
    // The copy starts now. A begin sent after the close would open the run again, with nothing left to close it.
    copy.go();
    assert.ok(await recorded(box), `left behind: ${handedOff(box)}`);
    assert.deepEqual(runOps(), ["run.begin", "run.complete"]);
    assert.deepEqual(
      withTmpdir(box.tmp, () => readRunState(SESSION)),
      { run_id: `cc-${SESSION}` },
    );
  } finally {
    copy.go();
    await loom.close();
    await removeSandbox(box);
  }
});

test("a Stop tells the verdict the prompt's copy stored, once, and asks the server nothing", async () => {
  const box = sandbox();
  indexed(box);
  // The prompt's lookups are answered at once; its recording calls are held.
  const loom = await heldLoom({ replies: { "namespace.audit": AUDIT }, now: ["code.search", "context.pack"] });
  const stop = () =>
    runLauncher(["Stop"], env(box, loom.url), payload("Stop", box, { stop_hook_active: false }), { script: HEALTH });
  const audits = () => loom.ops.filter((op) => op === "namespace.audit").length;
  try {
    const r = await runLauncher(["UserPromptSubmit"], env(box, loom.url), payload("UserPromptSubmit", box, PROMPT));
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    await arrived(loom, ["namespace.audit"]);
    // The copy is still asking: this Stop passes, and waits for no answer.
    assert.deepEqual(await stop(), { code: 0, stdout: "", stderr: "" });
    loom.release();
    assert.ok(await recorded(box), `left behind: ${handedOff(box)}`);
    const told = await stop();
    assert.equal(told.code, 0);
    assert.deepEqual(JSON.parse(told.stdout), {
      decision: "block",
      reason: "loom: namespace agent-test has 1 critical usage finding (exact_key_store) — run /loom-optimize",
    });
    assert.deepEqual(await stop(), { code: 0, stdout: "", stderr: "" });
    assert.equal(audits(), 1, "the session asks once");
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("where no copy asked, the first Stop asks and later ones pass", async () => {
  const box = sandbox();
  const loom = await heldLoom({ replies: { "namespace.audit": AUDIT } });
  loom.release();
  const stop = () =>
    runLauncher(["Stop"], env(box, loom.url), payload("Stop", box, { stop_hook_active: false }), { script: HEALTH });
  try {
    assert.match(JSON.parse((await stop()).stdout).reason, /exact_key_store/);
    assert.deepEqual(await stop(), { code: 0, stdout: "", stderr: "" });
    assert.deepEqual(loom.ops, ["namespace.audit"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("a Stop asks in place when the prompt's copy asked long ago and left no verdict", async () => {
  const box = sandbox();
  const loom = await heldLoom({ replies: { "namespace.audit": AUDIT } });
  loom.release();
  const stop = () =>
    runLauncher(
      ["Stop"],
      env(box, loom.url, { LOOM_HOOK_TIMEOUT_MS: "8000" }),
      payload("Stop", box, { stop_hook_active: false }),
      { script: HEALTH },
    );
  const asked = (ago) => writeJsonFile(sessionStatePath("nshealth", SESSION, { tmp: box.tmp }), { asked_at: ago });
  try {
    // Past the 8 s bound, inside twice that: a copy that looked the checkout up
    // before a slow request can still be at work, and is not asked over.
    asked(Date.now() - 9000);
    assert.deepEqual(await stop(), { code: 0, stdout: "", stderr: "" });
    assert.deepEqual(loom.ops, []);
    // A copy killed between its ask and its answer leaves this behind.
    asked(Date.now() - 60000);
    assert.match(JSON.parse((await stop()).stdout).reason, /exact_key_store/);
    assert.deepEqual(await stop(), { code: 0, stdout: "", stderr: "" });
    assert.deepEqual(loom.ops, ["namespace.audit"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("the SessionEnd hook returns with its records sent, and the run's close does not end the scripts beside it", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  loom.release(); // run.complete is awaited: answer at once
  try {
    withTmpdir(box.tmp, () => writeRunBegun(SESSION, `cc-${SESSION}`));
    // A hand-off, had there been one, would stay: the copy dies at its start.
    const r = await runHook("SessionEnd", box, loom.url, dyingCopy(box));
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    // Nothing is left to a process that a torn-down host would end first.
    assert.deepEqual(handedOff(box), []);
    for (const op of ["observe.crystallize", "run.complete"]) assert.ok(loom.ops.includes(op), `${op} not sent yet`);
    // The conversation hook reads the transcript only after its settle, which
    // outlasts the run's close; it, session memory and the brief go through
    // the flusher.
    await arrived(loom, ["memory.set_from_messages", "conversation.append", "session.brief_save"]);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

test("the SessionEnd hook does not wait on a stdin its host leaves open", async () => {
  const box = sandbox();
  const loom = await heldLoom();
  loom.release();
  try {
    withTmpdir(box.tmp, () => writeRunBegun(SESSION, `cc-${SESSION}`));
    // An exit grace no test waits out: the hook returns when its records are sent.
    const hookEnv = env(box, loom.url, { LOOM_HOOK_EXIT_MS: "60000" });
    const started = Date.now();
    const r = await runLauncher(["SessionEnd"], hookEnv, payload("SessionEnd", box), { leaveOpen: true });
    const waited = Date.now() - started;
    assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
    assert.ok(loom.ops.includes("run.complete"), `run.complete not sent; seen: ${loom.ops.join(", ")}`);
    // The launcher's own bound ends a process that stdin holds open at 16 s.
    assert.ok(waited < 10000, `the hook returned after ${waited} ms`);
  } finally {
    await loom.close();
    await removeSandbox(box);
  }
});

// SessionEnd: five scripts in the hook ask for the scope. A prompt: two
// lookups in the hook, two recorders in the copy. Asked once per script,
// their git calls would keep the first script's request from leaving.
for (const [event, processes] of [
  ["SessionEnd", 1],
  ["UserPromptSubmit", 2],
]) {
  test(`at ${event}, scripts that share a process look the checkout up once`, {
    skip: process.platform === "win32",
  }, async () => {
    const box = sandbox();
    indexed(box);
    const loom = await heldLoom();
    loom.release();
    // A git first on PATH that notes each call and runs the real one.
    const bin = path.join(box.dir, "bin");
    const calls = path.join(box.dir, "git-calls");
    fs.mkdirSync(bin);
    fs.writeFileSync(path.join(bin, "git"), '#!/bin/sh\necho "$*" >> "$GIT_CALLS"\nPATH="$REAL_PATH" exec git "$@"\n', {
      mode: 0o755,
    });
    const shim = { PATH: `${bin}${path.delimiter}${process.env.PATH}`, REAL_PATH: process.env.PATH, GIT_CALLS: calls };
    try {
      const r = await runLauncher([event], env(box, loom.url, shim), payload(event, box, PROMPT));
      assert.deepEqual(r, { code: 0, stdout: "", stderr: "" });
      assert.ok(await recorded(box), `left behind: ${handedOff(box)}`);
      const lookups = fs.readFileSync(calls, "utf8").split("\n");
      assert.equal(
        lookups.filter((line) => line === "rev-parse --show-toplevel").length,
        processes,
        lookups.join("; "),
      );
    } finally {
      await loom.close();
      await removeSandbox(box);
    }
  });
}

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
