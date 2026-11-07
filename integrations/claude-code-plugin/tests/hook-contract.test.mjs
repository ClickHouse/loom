// Host contract for every wired hook: whatever the payload, the token or the
// server does, a hook exits 0, writes nothing to stderr, finishes within its
// bound, never echoes the bearer token, and prints either nothing or the
// output shape its event allows. Each case runs the real script as the host
// does (`node <script> <Event>`, payload on stdin) against a fake Loom.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hostEnv } from "./host-env.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "hooks", "hooks.json"), "utf8"));
const generic = JSON.parse(fs.readFileSync(path.join(root, "hooks", "hooks.generic.json"), "utf8"));

/** [{event, script}] for every command hooks.json wires. */
function wired() {
  const out = [];
  for (const [event, groups] of Object.entries(manifest.hooks)) {
    for (const g of groups) {
      for (const h of g.hooks) {
        // The sh line of the command: hooks/<script>" <Event> ends it.
        const m = h.command.match(/hooks\/([a-z-]+\.mjs)" (\w+)$/m);
        assert.ok(m, `unparsed command: ${h.command}`);
        assert.equal(m[2], event, `${h.command} names the event it is wired under`);
        out.push({ event, script: m[1] });
      }
    }
  }
  return out;
}

/** [{event, script, generic: true}] for every command hooks.generic.json
 *  names: a client that wires these may send the alternate field names. */
function wiredGeneric() {
  return Object.values(generic.events).map(({ command }) => {
    const m = command.match(/hooks\/([a-z-]+\.mjs) (\w+)$/);
    assert.ok(m, `unparsed generic command: ${command}`);
    return { event: m[2], script: m[1], generic: true };
  });
}

const HOOKS = wired();
const GENERIC_HOOKS = wiredGeneric();
/** Every wired (script, event), once per payload spelling it can receive. */
const ALL_HOOKS = [...HOOKS, ...GENERIC_HOOKS];
const TOKEN_SECRET = "s3cr3t-signature-never-printed";
const TOKEN = [
  Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url"),
  Buffer.from(JSON.stringify({ org: "acme", namespaces: ["agent-test"], sub: "tester" })).toString("base64url"),
  TOKEN_SECRET,
].join(".");

/** A fake Loom: `mode` is ok | unauthorized | hang. Records the ops asked. */
async function fakeLoom(mode) {
  const ops = [];
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    ops.push(req.url);
    if (mode === "hang") return; // accept, never answer
    req.resume();
    req.on("end", () => {
      if (mode === "unauthorized") {
        res.writeHead(401, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "unauthenticated" }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ rendered: "", results: [], repos: [] }));
    });
  });
  server.on("connection", (s) => {
    sockets.add(s);
    s.on("close", () => sockets.delete(s));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}`;
  const close = () =>
    new Promise((r) => {
      for (const s of sockets) s.destroy();
      server.close(() => r());
    });
  return { url, ops, close };
}

function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-hook-contract-"));
  const home = path.join(dir, "home");
  const tmp = path.join(dir, "tmp");
  const cwd = path.join(dir, "work");
  for (const d of [home, tmp, cwd]) fs.mkdirSync(d);
  const transcript = path.join(dir, "session.jsonl");
  // A git checkout with a remote, so the hooks that read the repository run
  // their full path.
  execFileSync("git", ["init", "-q"], { cwd });
  execFileSync("git", ["remote", "add", "origin", "git@github.com:example/contract.git"], { cwd });
  const lines = [
    { type: "user", uuid: "u1", message: { role: "user", content: "fix the flaky parser test please" } },
    {
      type: "assistant",
      uuid: "a0",
      message: {
        id: "m0",
        model: "claude-test",
        role: "assistant",
        content: [{ type: "tool_use", id: "t1", name: "Edit", input: { file_path: path.join(cwd, "a.txt") } }],
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
  fs.writeFileSync(transcript, lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  return { dir, home, tmp, cwd, transcript };
}

function payloadFor(event, box) {
  return {
    hook_event_name: event,
    session_id: "contract-session-1",
    transcript_path: box.transcript,
    cwd: box.cwd,
    prompt: "fix the flaky parser test please",
    tool_name: event.startsWith("Pre") || event.startsWith("Post") ? "Read" : undefined,
    tool_input: { file_path: path.join(box.cwd, "a.txt") },
    tool_response: { content: "hello" },
    stop_hook_active: false,
    reason: "exit",
    source: "startup",
  };
}

/** The same payload under the alternate names a generic client may send
 *  (hooks.generic.json promises the scripts accept them). */
function genericPayloadFor(event, box) {
  const p = payloadFor(event, box);
  return {
    sessionId: p.session_id,
    transcriptPath: p.transcript_path,
    cwd: p.cwd,
    user_prompt: p.prompt,
    toolName: p.tool_name,
    toolInput: p.tool_input,
    tool_result: p.tool_response,
    reason: p.reason,
  };
}

/** The payload a hook receives from the manifest that wires it. */
function stdinFor(h, box) {
  return JSON.stringify(h.generic ? genericPayloadFor(h.event, box) : payloadFor(h.event, box));
}

/** Run one hook; resolves {code, signal, stdout, stderr, ms}. */
function runHook({ script, event, stdin, env, closeStdin = true }) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [path.join(root, "hooks", script), event], {
      env,
      // A payload with no cwd falls back to the hook's own: the sandbox's
      // home, not the large checkout the suite runs from, whose git calls a
      // loaded runner stretches past the bound.
      cwd: env.HOME,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    const killer = setTimeout(() => child.kill("SIGKILL"), 20000);
    child.on("close", (code, signal) => {
      clearTimeout(killer);
      resolve({ code, signal, stdout, stderr, ms: Date.now() - started });
    });
    child.stdin.on("error", () => {});
    if (stdin !== undefined) child.stdin.write(stdin);
    if (closeStdin) child.stdin.end();
  });
}

function baseEnv(box, serverUrl, extra = {}) {
  const env = hostEnv({
    PATH: process.env.PATH,
    HOME: box.home,
    TMPDIR: box.tmp,
    LOOM_BASE_URL: serverUrl,
    LOOM_AUTO_SIGNUP: "off",
    LOOM_CONV_SETTLE_MS: "0",
    LOOM_HOOK_TIMEOUT_MS: "700",
    // The shunt's repository lookup has its own bound, 8 s by default: 1000,
    // the floor resolveTimeoutMs allows, keeps a hanging server's cost near
    // the other hooks'. code-hooks-failopen.test.mjs holds the hook to its
    // deadline (4 s in all with a 1000 ms lookup bound).
    LOOM_SHUNT_RESOLVE_TIMEOUT_MS: "1000",
    // A hook's swallowed error then reaches stderr, which every case checks.
    LOOM_HOOK_DEBUG: "on",
    ...extra,
  });
  return env;
}

/** The output channel each event allows: SessionStart prints primer text;
 *  every other event prints nothing or one JSON object. */
function assertOutputShape(event, script, stdout) {
  const out = stdout.trim();
  if (!out || event === "SessionStart") return;
  let parsed;
  assert.doesNotThrow(() => (parsed = JSON.parse(out)), `${script} ${event}: stdout is not JSON: ${out.slice(0, 200)}`);
  assert.equal(typeof parsed, "object", `${script} ${event}`);
}

async function runAll(cases, limit = 8) {
  const results = [];
  let i = 0;
  const lanes = Array.from({ length: limit }, async () => {
    while (i < cases.length) {
      const c = cases[i++];
      results.push({ c, r: await runHook(c) });
    }
  });
  await Promise.all(lanes);
  return results;
}

function assertContract(results, maxMs) {
  const failures = [];
  for (const { c, r } of results) {
    const label = `${c.script} ${c.event}${c.generic ? " (generic)" : ""} [${c.label}]`;
    const check = (ok, why) => ok || failures.push(`${label}: ${why}`);
    check(r.signal === null, `killed after ${r.ms} ms`);
    check(r.code === 0, `exit ${r.code}; stderr=${r.stderr.slice(0, 300)}`);
    check(r.stderr === "", `wrote to stderr: ${r.stderr.slice(0, 300)}`);
    check(r.ms < maxMs, `took ${r.ms} ms (bound ${maxMs})`);
    check(!r.stdout.includes(TOKEN_SECRET), "printed the token");
    try {
      assertOutputShape(c.event, c.script, r.stdout);
    } catch (e) {
      failures.push(e.message);
    }
  }
  assert.deepEqual(failures, []);
}

test("hooks.json and hooks.generic.json wire only scripts that exist, each under the event it names", () => {
  assert.ok(HOOKS.length >= 20);
  assert.ok(GENERIC_HOOKS.length >= 10);
  for (const { script } of ALL_HOOKS) assert.ok(fs.existsSync(path.join(root, "hooks", script)), script);
  const events = new Set(HOOKS.map((h) => h.event));
  for (const { event, script } of GENERIC_HOOKS) assert.ok(events.has(event), `${script}: ${event} is a host event`);
});

test("malformed, empty and non-object payloads: every hook exits 0 silently", async () => {
  const box = sandbox();
  const loom = await fakeLoom("ok");
  try {
    const cases = [];
    const seen = new Set();
    for (const h of ALL_HOOKS) {
      const id = `${h.script} ${h.event}`;
      if (seen.has(id)) continue; // a malformed payload has no spelling
      seen.add(id);
      for (const [label, stdin] of [
        ["garbage", "{not json"],
        ["empty", ""],
        ["array", "[1,2,3]"],
        ["wrong types", JSON.stringify({ session_id: 42, transcript_path: 7, cwd: 3, prompt: {}, tool_name: 9 })],
      ]) {
        cases.push({ ...h, label, stdin, env: baseEnv(box, loom.url, { LOOM_API_KEY: TOKEN }) });
      }
    }
    assertContract(await runAll(cases), 8000);
  } finally {
    await loom.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("no token anywhere: every hook exits 0 silently and sends no request", async () => {
  const box = sandbox();
  const loom = await fakeLoom("ok");
  try {
    const cases = ALL_HOOKS.map((h) => ({
      ...h,
      label: "no token",
      stdin: stdinFor(h, box),
      env: baseEnv(box, loom.url),
    }));
    const results = await runAll(cases);
    assertContract(results, 8000);
    await new Promise((r) => setTimeout(r, 300)); // let a detached flusher try
    assert.deepEqual(loom.ops, [], "a hook without a token reached the server");
  } finally {
    await loom.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("server unreachable, answering 401, or hanging: every hook stays within its bound", async () => {
  const box = sandbox();
  const unauthorized = await fakeLoom("unauthorized");
  const hang = await fakeLoom("hang");
  try {
    const cases = [];
    for (const h of ALL_HOOKS) {
      const stdin = stdinFor(h, box);
      const env = (url) => baseEnv(box, url, { LOOM_API_KEY: TOKEN });
      cases.push({ ...h, label: "unreachable", stdin, env: env("http://127.0.0.1:9") });
      cases.push({ ...h, label: "401", stdin, env: env(unauthorized.url) });
      cases.push({ ...h, label: "hang", stdin, env: env(hang.url) });
    }
    // A hanging server costs a hook at most a few request bounds (700 ms
    // here, and the shunt's lookup at its 1000 ms floor) plus node start-up;
    // far below any host timeout. The shunt's shipped lookup bound (8 s, a
    // 12 s deadline) is not run here.
    assertContract(await runAll(cases), 8000);
  } finally {
    await unauthorized.close();
    await hang.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("a host that never closes stdin does not hold a hook open", async () => {
  const box = sandbox();
  const loom = await fakeLoom("ok");
  try {
    const cases = ALL_HOOKS.map((h) => ({
      ...h,
      label: "stdin left open",
      stdin: stdinFor(h, box),
      closeStdin: false,
      env: baseEnv(box, loom.url, { LOOM_API_KEY: TOKEN }),
    }));
    assertContract(await runAll(cases), 8000);
  } finally {
    await loom.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("a well-formed session against a healthy server: every hook exits 0 silently", async () => {
  const box = sandbox();
  const loom = await fakeLoom("ok");
  try {
    const cases = ALL_HOOKS.map((h) => ({
      ...h,
      label: h.generic ? "ok, generic spelling" : "ok",
      stdin: stdinFor(h, box),
      env: baseEnv(box, loom.url, { LOOM_API_KEY: TOKEN }),
    }));
    assertContract(await runAll(cases), 8000);
    assert.ok(loom.ops.length > 0, "no hook reached a healthy server");
  } finally {
    await loom.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("a payload over the stdin cap is dropped, not parsed: every hook exits 0 silently", async () => {
  const box = sandbox();
  const loom = await fakeLoom("ok");
  try {
    // 33 M characters of one JSON string: past the 32 M cap, well-formed if read.
    const big = JSON.stringify({ ...payloadFor("Stop", box), pad: "x".repeat(33 * 1024 * 1024) });
    const cases = [...new Map(ALL_HOOKS.map((h) => [`${h.script} ${h.event}`, h])).values()].map((h) => ({
      ...h,
      label: "over the cap",
      stdin: big,
      env: baseEnv(box, loom.url, { LOOM_API_KEY: TOKEN }),
    }));
    assertContract(await runAll(cases, 4), 15000);
  } finally {
    await loom.close();
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});
