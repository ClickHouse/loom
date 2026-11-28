#!/usr/bin/env node
// Offline measurement of the plugin's hook wall time: no client, no model, no
// Loom server. Drives the hooks a plugin directory wires in hooks/hooks.json
// the way Claude Code does (one `node <script> <Event>` per wired hook, the
// event's hooks in parallel, the payload on stdin, the event over when the
// last process exits) through a scripted session, against a stub server that
// answers every op after a fixed delay and reports the checkout as indexed.
// A preload (proclog.cjs) logs every node process at its start and exit,
// detached ones included, which gives the process counts.
//
//   node tests/bench/hook-wall-time.mjs --plugin <plugin dir> [--latency 300]
//        [--tools 55] [--stops 3] [--runs 5] [--settle <ms>] [--json out.json]
//
// To compare two revisions, run it on each one's plugin directory (for
// `main`: `git archive origin/main integrations/claude-code-plugin | tar -x
// -C <dir>`).
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

function args(argv) {
  const o = { plugin: "", latency: 300, tools: 55, stops: 3, runs: 5, json: "", settle: "" };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i].replace(/^--/, "");
    if (!(k in o)) throw new Error(`unknown argument ${argv[i]}`);
    o[k] = typeof o[k] === "number" ? Number(argv[++i]) : argv[++i];
  }
  if (!o.plugin) throw new Error("--plugin <dir> is required");
  o.plugin = path.resolve(o.plugin);
  return o;
}

const TOKEN = [
  Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url"),
  Buffer.from(JSON.stringify({ org: "acme", namespaces: ["bench"], sub: "bench" })).toString("base64url"),
  "sig",
].join(".");

async function stub(latencyMs) {
  const ops = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (d) => (raw += d));
    req.on("end", () => {
      const op = (req.url || "").replace(/^\/v1\//, "");
      let body = {};
      try {
        body = JSON.parse(raw || "{}");
      } catch {
        /* recorded as an empty body */
      }
      ops.push({ op, at: Date.now(), session: body.session_id || body.thread_id || "" });
      let answer = { rendered: "", results: [], repos: [] };
      if (op === "code.repos") {
        answer = { repos: [{ repo: "example/bench", org: "acme", namespace: "code-bench", branch: "main" }] };
      } else if (op === "code.index_status") answer = { path_prefixes: [], git_head: "" };
      else if (op === "run.begin") answer = { run_id: body.run_id };
      else if (op === "code.test_gate") answer = { tests: [], untested: [] };
      setTimeout(() => {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify(answer));
      }, latencyMs);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { url: `http://127.0.0.1:${server.address().port}`, ops, close: () => new Promise((r) => server.close(r)) };
}

function sandbox() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-hook-bench-")));
  const home = path.join(dir, "home");
  const tmp = path.join(dir, "tmp");
  const cwd = path.join(dir, "work");
  for (const d of [path.join(home, ".loom"), tmp, cwd]) fs.mkdirSync(d, { recursive: true });
  fs.writeFileSync(path.join(home, ".loom", "token"), TOKEN, { mode: 0o600 });
  const git = (...a) => execFileSync("git", a, { cwd, stdio: "ignore" });
  git("init", "-q", "-b", "main");
  git("remote", "add", "origin", "https://github.com/example/bench.git");
  fs.mkdirSync(path.join(cwd, "src"));
  for (let i = 0; i < 8; i++) fs.writeFileSync(path.join(cwd, "src", `m${i}.py`), `def f${i}():\n    return ${i}\n`);
  git("add", "-A");
  git("-c", "user.name=bench", "-c", "user.email=bench@example.com", "commit", "-q", "-m", "base");
  return { dir, home, tmp, cwd, transcript: path.join(dir, "session.jsonl"), proclog: path.join(dir, "proc.jsonl") };
}

/** The hooks hooks.json wires for `event` whose matcher takes `tool`. */
function wired(manifest, event, tool) {
  const out = [];
  for (const g of manifest.hooks[event] || []) {
    const m = g.matcher;
    if (m && m !== "*" && !(tool && new RegExp(`^(?:${m})$`).test(tool))) continue;
    for (const h of g.hooks) out.push(h);
  }
  return out;
}

function runOne(h, plugin, env, stdin) {
  return new Promise((resolve) => {
    const started = performance.now();
    const argv = h.args.map((a) => a.replace("${CLAUDE_PLUGIN_ROOT}", plugin));
    const child = spawn(process.execPath, argv, { env, cwd: env.BENCH_CWD, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("close", (code) =>
      resolve({ script: path.basename(argv[0]), ms: performance.now() - started, code, stdout, stderr }),
    );
    child.stdin.on("error", () => {});
    child.stdin.end(stdin);
  });
}

async function session(o, manifest, server) {
  const box = sandbox();
  const sid = `bench-${process.pid}-${Date.now()}`;
  const env = {
    PATH: process.env.PATH,
    HOME: box.home,
    TMPDIR: box.tmp,
    BENCH_CWD: box.cwd,
    CLAUDE_PLUGIN_ROOT: o.plugin,
    CLAUDE_PLUGIN_OPTION_SERVER_URL: server.url,
    LOOM_AUTO_SIGNUP: "off",
    NODE_OPTIONS: `--require ${path.join(here, "proclog.cjs")}`,
    LOOM_BENCH_PROCLOG: box.proclog,
    ...(o.settle === "" ? {} : { LOOM_CONV_SETTLE_MS: o.settle }),
  };
  const events = []; // {event, tool, ms, hooks: [{script, ms}]}
  let n = 0;
  const line = (obj) =>
    fs.appendFileSync(
      box.transcript,
      JSON.stringify({ uuid: `u${n++}`, timestamp: new Date().toISOString(), sessionId: sid, ...obj }) + "\n",
    );
  const fire = async (event, extra = {}, tool = "") => {
    const hooks = wired(manifest, event, tool);
    if (!hooks.length) return;
    const stdin = JSON.stringify({
      hook_event_name: event,
      session_id: sid,
      transcript_path: box.transcript,
      cwd: box.cwd,
      ...extra,
    });
    const started = performance.now();
    const done = await Promise.all(hooks.map((h) => runOne(h, o.plugin, env, stdin)));
    const ms = performance.now() - started;
    for (const d of done) {
      if (d.code !== 0 || d.stderr.trim()) throw new Error(`${d.script} ${event}: exit ${d.code} ${d.stderr}`);
    }
    events.push({
      event,
      tool,
      ms,
      hooks: done.map((d) => ({ script: d.script, ms: d.ms })),
      out: done.filter((d) => d.stdout.trim()).map((d) => d.script),
    });
  };

  // 43 Bash, 8 Edit and 4 Read per 55 calls, spread evenly.
  const kinds = [];
  for (let i = 0; i < o.tools; i++) kinds.push(i % 7 === 3 ? "Edit" : i % 14 === 6 ? "Read" : "Bash");
  const stopAfter = new Set();
  for (let s = 1; s <= o.stops; s++) stopAfter.add(Math.ceil((o.tools * s) / o.stops) - 1);

  await fire("SessionStart", { source: "startup" });
  let prompt = true;
  for (let i = 0; i < kinds.length; i++) {
    if (prompt) {
      const text = `Fix the blueprint registration so nested names resolve, part ${i}. Keep the public behaviour.`;
      await fire("UserPromptSubmit", { prompt: text });
      line({ type: "user", message: { role: "user", content: text } });
      prompt = false;
    }
    const tool = kinds[i];
    const file = path.join(box.cwd, "src", `m${i % 8}.py`);
    const input =
      tool === "Bash"
        ? { command: `python3 -m pytest tests/test_m${i % 8}.py -q`, description: "run tests" }
        : tool === "Read"
          ? { file_path: file, offset: 1, limit: 40 }
          : { file_path: file, old_string: "return", new_string: "return 1 +" };
    line({
      type: "assistant",
      message: {
        id: `msg_${i}`,
        model: "claude-bench",
        role: "assistant",
        content: [{ type: "tool_use", id: `toolu_${i}`, name: tool, input }],
        usage: { input_tokens: 900, output_tokens: 120, cache_read_input_tokens: 17000 },
      },
    });
    await fire("PreToolUse", { tool_name: tool, tool_input: input }, tool);
    if (tool === "Edit") fs.appendFileSync(file, `# edit ${i}\nx${i} = ${i}\n`);
    const response =
      tool === "Bash"
        ? { stdout: "60 passed in 0.41s\n".repeat(3), stderr: "", interrupted: false }
        : tool === "Read"
          ? { type: "text", file: { filePath: file, content: fs.readFileSync(file, "utf8") } }
          : { filePath: file, structuredPatch: [] };
    line({
      type: "user",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: `toolu_${i}`, content: "ok" }] },
    });
    await fire("PostToolUse", { tool_name: tool, tool_input: input, tool_response: response }, tool);
    if (stopAfter.has(i)) {
      const reply = `Done with part ${i}: the registration now resolves nested names and the tests pass.`;
      line({
        type: "assistant",
        message: {
          id: `msg_s${i}`,
          model: "claude-bench",
          role: "assistant",
          content: [{ type: "text", text: reply }],
          usage: { input_tokens: 950, output_tokens: 60, cache_read_input_tokens: 17500 },
        },
      });
      await fire("Stop", { stop_hook_active: false, last_assistant_message: reply });
      prompt = true;
    }
  }
  await fire("SessionEnd", { reason: "exit" });
  const hostDone = Date.now();

  // Detached processes: wait until every started process has exited.
  const procs = () => {
    const rows = fs.existsSync(box.proclog)
      ? fs
          .readFileSync(box.proclog, "utf8")
          .split("\n")
          .filter(Boolean)
          .map((l) => JSON.parse(l))
      : [];
    const starts = rows.filter((r) => r.k === "start");
    const exits = new Map(rows.filter((r) => r.k === "exit").map((r) => [r.pid, r]));
    return { starts, exits };
  };
  // A process that has just been started has not logged yet: the session is
  // quiet when every started process has exited, no job file is left, and
  // nothing new has appeared for half a second.
  const deadline = Date.now() + 60000;
  let quietSince = 0;
  let seen = -1;
  for (;;) {
    const p = procs();
    const jobs = fs.readdirSync(box.tmp).filter((f) => /^loom-(flush|record)-/.test(f)).length;
    const quiet = p.starts.every((s) => p.exits.has(s.pid)) && jobs === 0 && p.starts.length === seen;
    seen = p.starts.length;
    if (!quiet) quietSince = 0;
    else if (!quietSince) quietSince = Date.now();
    if ((quietSince && Date.now() - quietSince >= 500) || Date.now() > deadline) break;
    await new Promise((r) => setTimeout(r, 50));
  }
  const p = procs();
  const stranded = fs.readdirSync(box.tmp).filter((f) => /^loom-(flush|record)-/.test(f));
  const lastOpAt = server.ops.length ? Math.max(...server.ops.map((x) => x.at)) : hostDone;
  fs.rmSync(box.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  return {
    events,
    procs: p.starts.map((s) => ({ ...s, life: p.exits.get(s.pid)?.life ?? null })),
    unfinished: p.starts.filter((s) => !p.exits.has(s.pid)).length,
    stranded,
    drainMs: lastOpAt - hostDone,
  };
}

const median = (xs) => {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
};
const sum = (xs) => xs.reduce((a, b) => a + b, 0);

async function main() {
  const o = args(process.argv.slice(2));
  const manifest = JSON.parse(fs.readFileSync(path.join(o.plugin, "hooks", "hooks.json"), "utf8"));
  const runs = [];
  for (let r = 0; r < o.runs; r++) {
    const server = await stub(o.latency);
    let s;
    try {
      s = await session(o, manifest, server);
    } finally {
      await server.close();
    }
    const ops = {};
    for (const x of server.ops) ops[x.op] = (ops[x.op] || 0) + 1;
    runs.push({ ...s, ops: Object.fromEntries(Object.entries(ops).sort()) });
  }

  // Per event kind: wall time of one firing (median over all firings of all
  // runs) and the session total (median over runs).
  const kindOf = (e) => (e.tool ? `${e.event} ${e.tool}` : e.event);
  const kinds = [...new Set(runs.flatMap((r) => r.events.map(kindOf)))];
  const perEvent = kinds.map((k) => {
    const all = runs.flatMap((r) => r.events.filter((e) => kindOf(e) === k));
    return {
      kind: k,
      firings: all.length / runs.length,
      processes: all[0].hooks.length,
      medianMs: median(all.map((e) => e.ms)),
      sessionMs: median(runs.map((r) => sum(r.events.filter((e) => kindOf(e) === k).map((e) => e.ms)))),
    };
  });
  const scripts = {};
  for (const r of runs)
    for (const e of r.events) for (const h of e.hooks) (scripts[`${h.script} ${e.event}`] ||= []).push(h.ms);
  const perHook = Object.entries(scripts).map(([k, v]) => ({
    hook: k,
    n: v.length / runs.length,
    medianMs: median(v),
  }));
  // In-process view from the preload: node's own start-up, then the rest.
  const boot = median(runs.flatMap((r) => r.procs.map((p) => p.boot)));
  const waited = runs.map((r) => sum(r.events.map((e) => e.hooks.length)));
  const total = runs.map((r) => r.procs.length);
  const detachedByScript = {};
  for (const p of runs[0].procs) {
    const key = `${p.script} ${p.event.startsWith("/") ? "<job>" : p.event}`;
    detachedByScript[key] = (detachedByScript[key] || 0) + 1;
  }
  const out = {
    plugin: o.plugin,
    latencyMs: o.latency,
    tools: o.tools,
    stops: o.stops,
    runs: o.runs,
    sessionHookWallMs: median(runs.map((r) => sum(r.events.map((e) => e.ms)))),
    perEvent,
    perHook,
    nodeBootMs: boot,
    processesWaitedOn: median(waited),
    processesTotal: median(total),
    processesDetached: median(total) - median(waited),
    processesByScript: detachedByScript,
    unfinished: runs.map((r) => r.unfinished),
    stranded: runs.map((r) => r.stranded.length),
    drainMsAfterSessionEnd: median(runs.map((r) => r.drainMs)),
    ops: runs[0].ops,
    opsEveryRun: runs.every((r) => JSON.stringify(r.ops) === JSON.stringify(runs[0].ops)),
    injected: [...new Set(runs.flatMap((r) => r.events.flatMap((e) => e.out.map((s) => `${s} ${e.event}`))))],
  };
  if (o.json) fs.writeFileSync(o.json, JSON.stringify(out, null, 2));
  const f = (x) => x.toFixed(0).padStart(6);
  console.log(`plugin ${o.plugin}\nlatency ${o.latency} ms, ${o.tools} tool calls, ${o.stops} stops, ${o.runs} runs`);
  console.log(`\nevent                      firings  procs  median ms  session ms`);
  for (const e of perEvent)
    console.log(
      `${e.kind.padEnd(26)} ${String(e.firings).padStart(7)} ${String(e.processes).padStart(6)} ${f(e.medianMs)}     ${f(e.sessionMs)}`,
    );
  console.log(`session hook wall time: ${out.sessionHookWallMs.toFixed(0)} ms`);
  console.log(`\nhook process                              n   median ms`);
  for (const h of perHook.sort((a, b) => a.hook.localeCompare(b.hook)))
    console.log(`${h.hook.padEnd(40)} ${String(h.n).padStart(3)} ${f(h.medianMs)}`);
  console.log(`\nnode start-up (to first user code): ${boot.toFixed(0)} ms`);
  console.log(
    `processes: ${out.processesWaitedOn} waited on, ${out.processesDetached} detached, ${out.processesTotal} in all`,
  );
  console.log(`by script (run 1): ${JSON.stringify(detachedByScript)}`);
  console.log(
    `unfinished ${out.unfinished}, stranded job files ${out.stranded}, drain ${out.drainMsAfterSessionEnd} ms`,
  );
  console.log(`ops (run 1; same every run: ${out.opsEveryRun}): ${JSON.stringify(out.ops)}`);
  console.log(`printed something: ${JSON.stringify(out.injected)}`);
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
