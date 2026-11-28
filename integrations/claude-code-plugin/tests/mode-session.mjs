// One scripted session through the hooks a plugin directory wires in
// hooks/hooks.json, the way Claude Code starts them (one `node <script>
// <Event>` per wired hook, the payload on stdin), against a stub Loom server
// that reports the checkout as indexed and answers every other op with an
// empty result. No client, no model and no Loom server.
//
// The session: start, one prompt, a whole-file read, a one-file grep, an
// edit, a test run, a stop and the session's end. `runSession` returns the
// ops the stub received, and what the hooks printed for the first request:
// the SessionStart text and the prompt's additional context.
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { hostEnv } from "./host-env.mjs";

const PLUGIN = fileURLToPath(new URL("..", import.meta.url));

const claims = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const TOKEN = `${claims({ alg: "none" })}.${claims({ org: "acme", namespaces: ["dev"], sub: "dev" })}.sig`;

// A flusher job, or a payload handed to the recording launcher.
const JOB_RE = /^loom-(?:flush|record-[A-Za-z]+)-\d+-\d+\.json$/;

async function stub() {
  const ops = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      const op = (req.url || "").replace(/^\/v1\//, "");
      ops.push(op);
      let body = {};
      try {
        body = JSON.parse(raw || "{}");
      } catch {
        /* answered as an empty body */
      }
      let answer = {};
      if (op === "code.repos") {
        answer = {
          repos: [{ repo: "example/app", org: "acme", namespace: "code-app", branch: "main", readable: true }],
        };
      } else if (op === "code.index_status") {
        answer = { repo: "example/app", git_head: "abc", branch: "main", path_prefixes: [], indexed_files: 2 };
      } else if (op === "run.begin") answer = { run_id: body.run_id };
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(answer));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    ops,
    close: () => {
      server.closeAllConnections();
      server.close();
    },
  };
}

function sandbox() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-mode-session-")));
  const home = path.join(dir, "home");
  const tmp = path.join(dir, "tmp");
  const cwd = path.join(dir, "work");
  for (const d of [path.join(home, ".loom"), tmp, path.join(cwd, "src"), path.join(cwd, "tests")]) {
    fs.mkdirSync(d, { recursive: true });
  }
  fs.writeFileSync(path.join(home, ".loom", "token"), TOKEN, { mode: 0o600 });
  // Longer than the shunt's whole-file cap, so the read asks the index for an outline.
  const body = Array.from({ length: 900 }, (_, i) => `def f${i}():\n    return ${i}\n`).join("");
  fs.writeFileSync(path.join(cwd, "src", "app.py"), body);
  fs.writeFileSync(path.join(cwd, "tests", "test_app.py"), "def test_f0():\n    assert True\n");
  const git = (...a) => execFileSync("git", a, { cwd, stdio: "ignore" });
  git("init", "-q", "-b", "main");
  git("remote", "add", "origin", "https://github.com/example/app.git");
  git("add", "-A");
  git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base");
  return { dir, home, tmp, cwd, transcript: path.join(dir, "session.jsonl") };
}

/** The hooks hooks.json wires for `event` whose matcher takes `tool`. */
function wired(manifest, event, tool) {
  return (manifest.hooks[event] || [])
    .filter((g) => !g.matcher || g.matcher === "*" || (tool && new RegExp(`^(?:${g.matcher})$`).test(tool)))
    .flatMap((g) => g.hooks);
}

function runHook(hook, plugin, env, cwd, stdin) {
  return new Promise((resolve) => {
    const argv = hook.args.map((a) => a.replace("${CLAUDE_PLUGIN_ROOT}", plugin));
    const child = spawn(process.execPath, argv, { env, cwd, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) => resolve({ script: path.basename(argv[0], ".mjs"), code, stdout, stderr }));
    child.stdin.on("error", () => {});
    child.stdin.end(stdin);
  });
}

/** Every detached job is done and the stub has heard nothing for half a second. */
async function settled(box, server, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  let seen = -1;
  let quietSince = 0;
  for (;;) {
    const jobs = fs.readdirSync(box.tmp).filter((name) => JOB_RE.test(name));
    const quiet = jobs.length === 0 && server.ops.length === seen;
    seen = server.ops.length;
    if (!quiet) quietSince = 0;
    else if (!quietSince) quietSince = Date.now();
    if (quietSince && Date.now() - quietSince >= 500) return;
    if (Date.now() > deadline) throw new Error(`the session did not settle: ${JSON.stringify(jobs)}`);
    await delay(25);
  }
}

/**
 * Run the scripted session. `mode` is the plugin's `mode` option (unset when
 * undefined); `plugin` the plugin directory, this checkout's by default.
 * Resolves to {ops, startText, promptContext, outputs}: the distinct ops the
 * stub received, sorted, without the health probe; the SessionStart text;
 * the prompt's additional context; and the scripts that printed, per event.
 */
export async function runSession({ mode, plugin = PLUGIN, env: extra = {} } = {}) {
  const manifest = JSON.parse(fs.readFileSync(path.join(plugin, "hooks", "hooks.json"), "utf8"));
  const server = await stub();
  const box = sandbox();
  const sid = `mode-${process.pid}-${Date.now()}`;
  const env = hostEnv({
    PATH: process.env.PATH,
    HOME: box.home,
    TMPDIR: box.tmp,
    CLAUDE_PLUGIN_ROOT: plugin,
    CLAUDE_PLUGIN_OPTION_SERVER_URL: server.url,
    LOOM_AUTO_SIGNUP: "off",
    ...(mode === undefined ? {} : { CLAUDE_PLUGIN_OPTION_MODE: mode }),
    ...extra,
  });
  const outputs = [];
  let n = 0;
  const line = (obj) =>
    fs.appendFileSync(
      box.transcript,
      JSON.stringify({ uuid: `u${n++}`, timestamp: new Date().toISOString(), sessionId: sid, ...obj }) + "\n",
    );
  const fire = async (event, payload = {}, tool = "") => {
    const stdin = JSON.stringify({
      hook_event_name: event,
      session_id: sid,
      transcript_path: box.transcript,
      cwd: box.cwd,
      ...payload,
    });
    const done = await Promise.all(wired(manifest, event, tool).map((h) => runHook(h, plugin, env, box.cwd, stdin)));
    for (const d of done) {
      if (d.code !== 0 || d.stderr.trim()) throw new Error(`${d.script} ${event}: exit ${d.code} ${d.stderr}`);
      if (d.stdout.trim()) outputs.push({ event, tool, script: d.script, stdout: d.stdout });
    }
    return done;
  };
  const call = async (i, tool, input, response, act = () => {}) => {
    line({
      type: "assistant",
      message: {
        id: `msg_${i}`,
        model: "claude-test",
        role: "assistant",
        content: [{ type: "tool_use", id: `toolu_${i}`, name: tool, input }],
        usage: { input_tokens: 900, output_tokens: 120, cache_read_input_tokens: 17000 },
      },
    });
    await fire("PreToolUse", { tool_name: tool, tool_input: input }, tool);
    act();
    line({
      type: "user",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: `toolu_${i}`, content: "ok" }] },
    });
    await fire("PostToolUse", { tool_name: tool, tool_input: input, tool_response: response }, tool);
  };

  try {
    const start = await fire("SessionStart", { source: "startup" });
    const prompt = "Fix f1 in src/app.py so it returns its index plus one. Keep the public behaviour.";
    const asked = await fire("UserPromptSubmit", { prompt });
    line({ type: "user", message: { role: "user", content: prompt } });
    const file = path.join(box.cwd, "src", "app.py");
    await call(0, "Read", { file_path: file }, { type: "text", file: { filePath: file, content: "def f0():" } });
    await call(1, "Grep", { pattern: "f1", path: file, output_mode: "content" }, { content: "def f1():" });
    await call(
      2,
      "Edit",
      { file_path: file, old_string: "    return 1\n", new_string: "    return 1 + 1\n" },
      { filePath: file, structuredPatch: [] },
      () => fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace("    return 1\n", "    return 1 + 1\n")),
    );
    await call(
      3,
      "Bash",
      { command: "python3 -m pytest tests/test_app.py -q", description: "run the tests" },
      { stdout: "1 passed in 0.01s\n", stderr: "", interrupted: false },
    );
    const reply = "f1 now returns its index plus one, and the tests pass.";
    line({
      type: "assistant",
      message: {
        id: "msg_done",
        model: "claude-test",
        role: "assistant",
        content: [{ type: "text", text: reply }],
        usage: { input_tokens: 950, output_tokens: 60, cache_read_input_tokens: 17500 },
      },
    });
    await fire("Stop", { stop_hook_active: false, last_assistant_message: reply });
    await fire("SessionEnd", { reason: "exit" });
    await settled(box, server);
    const context = asked.map((d) => {
      try {
        return JSON.parse(d.stdout).hookSpecificOutput.additionalContext || "";
      } catch {
        return "";
      }
    });
    return {
      ops: [...new Set(server.ops)].filter((op) => op !== "health").sort(),
      startText: start.map((d) => d.stdout).join(""),
      promptContext: context.join(""),
      outputs,
    };
  } finally {
    server.close();
    fs.rmSync(box.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}
