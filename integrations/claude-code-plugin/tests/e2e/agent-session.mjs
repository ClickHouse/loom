#!/usr/bin/env node
// One real agent session with the Loom plugin installed, on whatever OS runs
// it: the agent CLI installs the plugin from this checkout into a scratch
// home, then runs one headless turn against a local fake that answers both
// the model API and Loom. The model's turn runs one shell command, so the
// session fires SessionStart, UserPromptSubmit, PreToolUse, PostToolUse and
// Stop through the shell the agent picks on this platform, and the MCP
// entry's header helper runs the way the agent runs it. No model provider
// is called.
//
//   node agent-session.mjs claude --bin <path to claude>
//   node agent-session.mjs codex  --bin <path to the native codex binary>
//     [--port 7777]         the fake's port; the Codex plugin ships localhost:7777
//     [--expect-tool PowerShell]  Claude Code's shell tool must be this one: without
//                           Git Bash it is PowerShell, which runs the hooks too
//     [--node-off-path]     the agent's PATH has no node, as for a GUI launch:
//                           the hooks' launcher must find it under a version
//                           manager (not on Windows: the stand-in nvm node is a sh script)
//
// Exit 0 when the helper sent the token, each event's own Loom call arrived
// with it, and (Claude Code, which reports them) every hook exited 0 with
// nothing on stderr; otherwise exit 1 naming what is missing.
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
const TOKEN = "loom-e2e-token";
const MODEL_KEY = "e2e-model-key";

function parseArgs(argv) {
  const out = { agent: argv[0], bin: "", port: 7777, nodeOffPath: false, expectTool: "" };
  for (let i = 1; i < argv.length; i++) {
    if (argv[i] === "--bin") out.bin = argv[++i];
    else if (argv[i] === "--port") out.port = Number(argv[++i]);
    else if (argv[i] === "--node-off-path") out.nodeOffPath = true;
    else if (argv[i] === "--expect-tool") out.expectTool = argv[++i];
    else throw new Error(`unknown argument ${argv[i]}`);
  }
  if (!["claude", "codex"].includes(out.agent) || !out.bin) {
    throw new Error(
      "usage: agent-session.mjs claude|codex --bin <path> [--port n] [--node-off-path] [--expect-tool <name>]",
    );
  }
  return out;
}

// The model's side: the first turn asks for one shell command, the turn
// that carries its output ends with text. Claude Code speaks the Messages
// API, Codex the Responses API; both stream server-sent events.
function sse(res, events) {
  res.writeHead(200, { "content-type": "text/event-stream" });
  for (const [type, data] of events) res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  res.end();
}

function messagesTurn(body, res) {
  const tools = (body.tools || []).map((t) => t.name);
  const answered = JSON.stringify(body.messages?.at(-1)?.content ?? "").includes("tool_result");
  const shell = ["Bash", "PowerShell"].find((t) => tools.includes(t));
  const message = {
    id: "msg_e2e",
    type: "message",
    role: "assistant",
    model: body.model,
    content: [],
    usage: { input_tokens: 1, output_tokens: 1 },
  };
  const block =
    !answered && shell
      ? { type: "tool_use", id: "toolu_e2e", name: shell, input: { command: "echo e2e-ok", description: "e2e" } }
      : { type: "text", text: "e2e done" };
  const stop = block.type === "tool_use" ? "tool_use" : "end_turn";
  if (!body.stream) {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ...message, content: [block], stop_reason: stop }));
    return;
  }
  const start = block.type === "text" ? { type: "text", text: "" } : { ...block, input: {} };
  const delta =
    block.type === "text"
      ? { type: "text_delta", text: block.text }
      : { type: "input_json_delta", partial_json: JSON.stringify(block.input) };
  sse(res, [
    ["message_start", { message }],
    ["content_block_start", { index: 0, content_block: start }],
    ["content_block_delta", { index: 0, delta }],
    ["content_block_stop", { index: 0 }],
    ["message_delta", { delta: { stop_reason: stop, stop_sequence: null }, usage: { output_tokens: 1 } }],
    ["message_stop", {}],
  ]);
}

function responsesTurn(body, res) {
  const tools = (body.tools || []).map((t) => t.name);
  const answered = JSON.stringify(body.input ?? "").includes("function_call_output");
  const args = {
    exec_command: { cmd: "echo e2e-ok" },
    shell_command: { command: "echo e2e-ok" },
    shell: { command: ["echo", "e2e-ok"] },
  };
  const name = Object.keys(args).find((t) => tools.includes(t));
  const item =
    !answered && name
      ? { type: "function_call", id: "fc_e2e", call_id: "call_e2e", name, arguments: JSON.stringify(args[name]) }
      : { type: "message", role: "assistant", id: "msg_e2e", content: [{ type: "output_text", text: "e2e done" }] };
  const usage = {
    input_tokens: 1,
    input_tokens_details: null,
    output_tokens: 1,
    output_tokens_details: null,
    total_tokens: 2,
  };
  sse(res, [
    ["response.created", { response: { id: "resp_e2e" } }],
    ["response.output_item.done", { item }],
    ["response.completed", { response: { id: "resp_e2e", usage } }],
  ]);
}

/** The fake: the model API under /v1/messages and /v1/responses, Loom for
 *  everything else (any op answers an empty success, MCP answers 401).
 *  `seen` records each request's path and Authorization header. */
async function startFake(port) {
  const seen = [];
  const handler = (req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      let body = {};
      try {
        body = JSON.parse(raw || "{}");
      } catch {
        /* a non-JSON body is recorded like any other */
      }
      const url = req.url || "";
      seen.push({ path: url.split("?")[0], auth: req.headers.authorization || "", mode: body.mode, repos: body.repos });
      if (url.startsWith("/v1/messages/count_tokens")) {
        res.writeHead(200, { "content-type": "application/json" });
        res.end('{"input_tokens":1}');
      } else if (url.startsWith("/v1/messages")) messagesTurn(body, res);
      else if (url.startsWith("/v1/responses")) responsesTurn(body, res);
      else if (url.startsWith("/mcp")) {
        // The helper's header is what is under test, not the MCP protocol.
        res.writeHead(401, { "content-type": "application/json" });
        res.end("{}");
      } else {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ rendered: "", results: [], repos: [] }));
      }
    });
  };
  // `localhost` resolves to ::1 first on some hosts: listen on both.
  const servers = [];
  for (const host of ["127.0.0.1", "::1"]) {
    const server = http.createServer(handler);
    try {
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, resolve);
      });
      servers.push(server);
    } catch (e) {
      // No IPv6 here: IPv4 alone serves. Anything else (the port taken) fails.
      if (host === "127.0.0.1" || !["EADDRNOTAVAIL", "EAFNOSUPPORT"].includes(e.code)) throw e;
    }
  }
  const close = () => Promise.all(servers.map((s) => new Promise((r) => s.close(r))));
  return { seen, close };
}

function run(cmd, args, { env, cwd, timeoutMs = 180000, input = "" }) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { env, cwd, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ status: -1, stdout, stderr: `${stderr}${e.message}` });
    });
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

/** A scratch home holding only the Loom token, with HOME and USERPROFILE
 *  both pointing at it. */
function scratch() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-e2e-"));
  const home = path.join(dir, "home");
  const work = path.join(dir, "work");
  fs.mkdirSync(path.join(home, ".loom"), { recursive: true });
  fs.mkdirSync(work);
  // A checkout, so the hooks resolve its root as on a user's machine (the
  // temp directory is /private/var on macOS, an 8.3 path on Windows). Best
  // effort: the no-Git-Bash step has no git at all.
  let checkout = true;
  try {
    execFileSync("git", ["init", "-q"], { cwd: work, stdio: "ignore", windowsHide: true });
    // A remote, so the hooks look the checkout's repository up.
    execFileSync("git", ["remote", "add", "origin", "https://github.com/example/e2e.git"], {
      cwd: work,
      stdio: "ignore",
      windowsHide: true,
    });
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
    checkout = false; // no git here: outside a checkout the hooks still run
  }
  fs.writeFileSync(path.join(home, ".loom", "token"), TOKEN, { mode: 0o600 });
  return { dir, home, work, checkout };
}

/** PATH without any directory holding node, and a home where node is found
 *  only as nvm's default install, as for an agent started from the Dock. That
 *  node notes each run's arguments (not its --version probe) in a file. */
function hideNode(env, home) {
  const exe = process.platform === "win32" ? "node.exe" : "node";
  const own = path.dirname(process.execPath);
  if (["/bin", "/usr/bin"].includes(own)) throw new Error(`--node-off-path needs node outside ${own}`);
  const key = Object.keys(env).find((k) => k.toUpperCase() === "PATH") || "PATH";
  env[key] = env[key]
    .split(path.delimiter)
    .filter((d) => d && !fs.existsSync(path.join(d, exe)))
    .join(path.delimiter);
  const bin = path.join(home, ".nvm", "versions", "node", `v${process.versions.node}`, "bin");
  fs.mkdirSync(bin, { recursive: true });
  const used = path.join(home, "nvm-node-used");
  fs.writeFileSync(
    path.join(bin, exe),
    `#!/bin/sh\n[ "$1" = --version ] || echo "$*" >> "${used}"\nexec "${process.execPath}" "$@"\n`,
    { mode: 0o755 },
  );
  fs.mkdirSync(path.join(home, ".nvm", "alias"), { recursive: true });
  fs.writeFileSync(path.join(home, ".nvm", "alias", "default"), `${process.versions.node}\n`);
  env.LOOM_NODE_SYSTEM_DIRS = "";
  // A runner's own version managers point elsewhere: only this home's nvm.
  for (const k of [
    "NVM_DIR",
    "NVM_BIN",
    "FNM_DIR",
    "VOLTA_HOME",
    "ASDF_DATA_DIR",
    "MISE_DATA_DIR",
    "NODENV_ROOT",
    "N_PREFIX",
  ]) {
    delete env[k];
  }
}

/** The JSON objects among an agent's output lines. */
function jsonLines(text) {
  const out = [];
  for (const line of text.split("\n")) {
    if (!line.startsWith("{")) continue;
    try {
      out.push(JSON.parse(line));
    } catch {
      /* a log line that starts with a brace */
    }
  }
  return out;
}

function baseEnv(home) {
  const env = { ...process.env, HOME: home, USERPROFILE: home };
  for (const k of Object.keys(env)) {
    if (/^(LOOM_|ANTHROPIC_|CLAUDE_|CODEX_|OPENAI_)/.test(k)) delete env[k];
  }
  return env;
}

async function claudeSession(opts, box, url) {
  const env = baseEnv(box.home);
  env.ANTHROPIC_API_KEY = MODEL_KEY;
  env.ANTHROPIC_BASE_URL = url;
  env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  env.DISABLE_AUTOUPDATER = "1";
  const setup = [
    ["plugin", "marketplace", "add", repoRoot],
    ["plugin", "install", "loom@loom", "--config", `server_url=${url}`],
  ];
  for (const args of setup) {
    const r = await run(opts.bin, args, { env, cwd: box.work, timeoutMs: 120000 });
    if (r.status !== 0) return { problems: [`claude ${args.join(" ")} exited ${r.status}: ${r.stderr}${r.stdout}`] };
  }
  if (opts.nodeOffPath) hideNode(env, box.home);
  const r = await run(
    opts.bin,
    [
      "-p",
      "run the e2e command",
      "--allowedTools",
      "Bash,PowerShell",
      "--output-format",
      "stream-json",
      "--verbose",
      "--include-hook-events",
    ],
    { env, cwd: box.work },
  );
  const problems = [];
  if (r.status !== 0) problems.push(`claude -p exited ${r.status}: ${r.stderr}`);
  const events = jsonLines(r.stdout);
  const hooks = events.filter((e) => e.type === "system" && e.subtype === "hook_response");
  const fired = new Set(hooks.map((h) => h.hook_event));
  // PreToolUse is wired for Read, Grep and Bash; without Git Bash the shell
  // tool is PowerShell, which no PreToolUse hook matches.
  const tool = events
    .flatMap((e) => (e.type === "assistant" ? e.message?.content || [] : []))
    .find((c) => c.type === "tool_use")?.name;
  const expected = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"];
  if (tool === "Bash") expected.push("PreToolUse");
  if (opts.expectTool && tool !== opts.expectTool)
    problems.push(`the turn's shell tool was ${tool}, not ${opts.expectTool}`);
  for (const ev of expected) {
    if (!fired.has(ev)) problems.push(`no ${ev} hook ran`);
  }
  // A hook's stdout reaches the agent through whichever shell ran it: the
  // primer is SessionStart's.
  if (!hooks.some((h) => h.hook_event === "SessionStart" && String(h.output || "").includes("# Loom"))) {
    problems.push("SessionStart's primer never reached Claude Code");
  }
  for (const h of hooks) {
    if (h.exit_code !== 0 || (h.stderr || "").trim()) {
      problems.push(`${h.hook_name} exited ${h.exit_code} with stderr: ${String(h.stderr || "").trim()}`);
    }
  }
  const ran = events.some(
    (e) =>
      e.type === "user" &&
      (e.message?.content || []).some(
        (c) => c.type === "tool_result" && !c.is_error && String(c.content).includes("e2e-ok"),
      ),
  );
  if (!ran) problems.push("the shell command did not run");
  return { problems, transcript: r.stdout + r.stderr };
}

async function codexSession(opts, box, url) {
  const env = baseEnv(box.home);
  env.E2E_MODEL_KEY = MODEL_KEY;
  // Codex on Windows takes its home from the profile folder, not USERPROFILE:
  // name it, where the header helper (which reads the home directory) looks.
  const codexHome = path.join(box.home, ".codex");
  fs.mkdirSync(codexHome, { recursive: true });
  env.CODEX_HOME = codexHome;
  const setup = [
    ["plugin", "marketplace", "add", repoRoot],
    ["plugin", "add", "loom@loom"],
  ];
  for (const args of setup) {
    const r = await run(opts.bin, args, { env, cwd: box.work, timeoutMs: 120000 });
    if (r.status !== 0) return { problems: [`codex ${args.join(" ")} exited ${r.status}: ${r.stderr}${r.stdout}`] };
  }
  // The model provider goes first: keys after a table header belong to it.
  const config = fs.readFileSync(path.join(codexHome, "config.toml"), "utf8");
  const provider = [
    'model = "gpt-5.1-codex"',
    'model_provider = "e2e"',
    "",
    "[model_providers.e2e]",
    'name = "e2e"',
    `base_url = "${url}/v1"`,
    'wire_api = "responses"',
    'env_key = "E2E_MODEL_KEY"',
    "supports_websockets = false",
    "",
  ].join("\n");
  fs.writeFileSync(path.join(codexHome, "config.toml"), `${provider}\n${config}`);
  if (opts.nodeOffPath) hideNode(env, box.home);
  const r = await run(
    opts.bin,
    [
      "exec",
      "--json",
      "--skip-git-repo-check",
      "--dangerously-bypass-approvals-and-sandbox",
      "--dangerously-bypass-hook-trust",
      "run the e2e command",
    ],
    { env, cwd: box.work },
  );
  const problems = [];
  if (r.status !== 0) problems.push(`codex exec exited ${r.status}: ${r.stderr}`);
  const ran = jsonLines(r.stdout).some(
    (e) =>
      e.type === "item.completed" &&
      e.item?.type === "command_execution" &&
      e.item.exit_code === 0 &&
      String(e.item.aggregated_output).includes("e2e-ok"),
  );
  if (!ran) problems.push("the shell command did not run");
  return { problems, transcript: r.stdout + r.stderr };
}

/** A Loom call only one event's hooks make, by that event: the primer's
 *  startup pack, the run the first prompt opens, the tool call's record, the
 *  turns the Stop reports. PreToolUse sends nothing outside an indexed
 *  checkout; Claude Code reports its hook results instead. */
function expectedCalls(agent) {
  return {
    SessionStart: (s) => s.path === "/v1/context.pack" && s.mode === "startup",
    UserPromptSubmit: (s) => s.path === "/v1/run.begin",
    PostToolUse: (s) => s.path === "/v1/observe.record",
    // Claude Code also appends the conversation at SessionEnd; its usage
    // hook runs at Stop alone.
    Stop: (s) => s.path === (agent === "claude" ? "/v1/cost.record_turns" : "/v1/conversation.append"),
  };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const fake = await startFake(opts.port);
  const url = `http://localhost:${opts.port}`;
  const box = scratch();
  const EXPECTED_CALLS = expectedCalls(opts.agent);
  let result;
  try {
    result = await (opts.agent === "claude" ? claudeSession : codexSession)(opts, box, url);
    // Stop hands its posts to a detached flusher: give it time to land.
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !fake.seen.some(EXPECTED_CALLS.Stop)) {
      await new Promise((r) => setTimeout(r, 250));
    }
    const used = path.join(box.home, "nvm-node-used");
    const ran = fs.existsSync(used) ? fs.readFileSync(used, "utf8") : "";
    if (opts.nodeOffPath && !/[\\/]hooks[\\/]loom-[a-z-]+\.mjs/.test(ran)) {
      result.problems.push("no hook ran the version manager's node");
    }
    const lookedUp = (s) => s.path === "/v1/code.repos" && JSON.stringify(s.repos || []).includes("example/e2e");
    if (box.checkout && !fake.seen.some(lookedUp)) {
      result.problems.push("no hook looked the checkout's repository up");
    }
  } finally {
    await fake.close();
    // An agent's own background process may still be writing under the home.
    try {
      fs.rmSync(box.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch {
      /* a temp directory the OS cleans */
    }
  }
  const { problems } = result;
  const bearer = `Bearer ${TOKEN}`;
  if (!fake.seen.some((s) => s.path === "/mcp" && s.auth === bearer)) {
    problems.push("the MCP header helper never sent the token");
  }
  for (const [event, call] of Object.entries(EXPECTED_CALLS)) {
    if (!fake.seen.some((s) => call(s) && s.auth === bearer))
      problems.push(`${event}: its Loom call never came with the token`);
  }
  const label = `${opts.agent} on ${process.platform}-${process.arch}${opts.nodeOffPath ? " (node off PATH)" : ""}`;
  if (problems.length) {
    console.error(`FAIL ${label}`);
    for (const p of problems) console.error(`  - ${p}`);
    const shown = fake.seen.map((s) => `${s.path}${s.mode ? `(${s.mode})` : ""}${s.auth ? " +auth" : ""}`);
    console.error("Loom requests:", shown.join(", "));
    console.error(result.transcript || "");
    process.exitCode = 1;
  } else {
    console.log(`PASS ${label}: ${fake.seen.length} requests`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
