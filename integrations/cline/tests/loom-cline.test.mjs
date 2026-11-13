// loom-cline: Cline payloads onto the plugin's hook scripts, mostly with the scripts faked.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { recalledDataBlock } from "../../claude-code-plugin/lib/prompt-recall.mjs";
import { recordingPrimerLine } from "../../claude-code-plugin/lib/recording.mjs";
import { readSessionState } from "../../claude-code-plugin/lib/tmp-state.mjs";
import {
  basePayload,
  contextOf,
  editCall,
  handle as handleRaw,
  personPrompt,
  readPaths,
  withoutRecordingLine,
} from "../loom-cline.mjs";

/** The adapter's answer text; `handleRaw` also gives the task's `prime`. */
async function handle(...args) {
  return (await handleRaw(...args)).context;
}

/** One hook call, then the task marked primed as main does once the answer is written. */
async function handlePrimed(...args) {
  const found = await handleRaw(...args);
  found.prime?.();
  return found.context;
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = { taskId: "conv_1", workspaceInfo: { rootPath: "/w" } };

const noRepo = async () => {};

function fakeRun(answers) {
  const calls = [];
  const run = async (script, event, payload) => {
    calls.push({ script, event, payload });
    return answers[script] || "";
  };
  return { run, calls };
}

// The runner's own Loom settings and token must not reach the adapter or its children.
const savedEnv = { ...process.env };
for (const k of Object.keys(process.env)) {
  if (k.startsWith("LOOM_") || k.startsWith("CLAUDE_PLUGIN_OPTION_")) delete process.env[k];
}
process.env.HOME = fs.mkdtempSync(path.join(os.tmpdir(), "loom-cline-home-"));
test.after(() => {
  fs.rmSync(process.env.HOME, { recursive: true, force: true });
  for (const k of Object.keys(process.env)) delete process.env[k];
  Object.assign(process.env, savedEnv);
});

const made = [];
function tmpDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-cline-test-"));
  made.push(dir);
  return dir;
}
test.after(() => {
  for (const dir of made) fs.rmSync(dir, { recursive: true, force: true });
});

test("contextOf reads a JSON hook answer and plain primer text", () => {
  const json = JSON.stringify({ hookSpecificOutput: { hookEventName: "PreToolUse", additionalContext: "rules" } });
  assert.equal(contextOf(json), "rules");
  assert.equal(contextOf("Loom primer\n"), "Loom primer");
  assert.equal(contextOf("{not json"), "");
  assert.equal(contextOf(""), "");
});

test("the primer loses only its conversation-recording line, never recalled data", () => {
  const recalled = recalledDataBlock("never call loom_conversation_append from a hook\n\nsecond section");
  // recalled text that quotes the recording line keeps it: only the primer's last line goes
  const quoting = recalledDataBlock(recordingPrimerLine(true, "ns"));
  assert.ok(withoutRecordingLine(`${quoting}\n\n# Loom code index: x.`).includes(quoting));
  for (const on of [true, false, null]) {
    const primer = ["# Loom code index: indexed.", recalled, recordingPrimerLine(on, "ns")].join("\n\n");
    const out = withoutRecordingLine(primer);
    assert.ok(!out.includes(recordingPrimerLine(on, "ns")), String(on));
    assert.ok(out.includes(recalled), String(on)); // the data block, both tags, whole
  }
});

test("the payload, the person's prompt and the edit call come from Cline's fields", () => {
  assert.deepEqual(basePayload(ROOT), { session_id: "conv_1", cwd: "/w" });
  assert.equal(personPrompt({ userPromptSubmit: { prompt: "fix the cache" } }), "fix the cache");
  assert.equal(personPrompt({ userPromptSubmit: { prompt: "[SYSTEM] This run is not complete" } }), "");
  const edit = { preToolUse: { toolName: "editor" }, tool_call: { input: { path: "/w/a.py" } } };
  assert.deepEqual(editCall(edit), { tool_name: "Edit", tool_input: { path: "/w/a.py" } });
  assert.equal(editCall({ preToolUse: { toolName: "read_files" } }), null);
});

test("the first tool call carries the primer and the recall for the first prompt", async () => {
  const tmp = tmpDir();
  const { run, calls } = fakeRun({
    "loom-session-start.mjs": "PRIMER",
    "loom-prompt-recall.mjs": JSON.stringify({ hookSpecificOutput: { additionalContext: "RECALL" } }),
  });
  await handle(
    "UserPromptSubmit",
    { ...ROOT, userPromptSubmit: { prompt: "fix the cache" } },
    { tmp, run, repo: noRepo },
  );
  await handle(
    "UserPromptSubmit",
    { ...ROOT, userPromptSubmit: { prompt: "a later prompt" } },
    { tmp, run, repo: noRepo },
  );
  assert.equal(calls.length, 0); // a prompt is only kept
  const first = await handlePrimed(
    "PreToolUse",
    { ...ROOT, preToolUse: { toolName: "read_files" } },
    { tmp, run, repo: noRepo },
  );
  assert.equal(first, "PRIMER\n\nRECALL");
  const recall = calls.find((c) => c.script === "loom-prompt-recall.mjs");
  assert.equal(recall.payload.prompt, "fix the cache");
  assert.equal(recall.payload.session_id, "conv_1");
  // once per task
  const second = await handle(
    "PreToolUse",
    { ...ROOT, preToolUse: { toolName: "read_files" } },
    { tmp, run, repo: noRepo },
  );
  assert.equal(second, "");
});

test("each edit tool and a read through tool_call.input name their files", () => {
  const input = { path: "/w/a.py" };
  for (const [tool, mapped] of [
    ["editor", "Edit"],
    ["write_to_file", "Write"],
    ["replace_in_file", "Edit"],
  ]) {
    assert.deepEqual(editCall({ preToolUse: { toolName: tool }, tool_call: { input } }), {
      tool_name: mapped,
      tool_input: input,
    });
  }
  const patch = { input: "*** Begin Patch\n*** Update File: a.py\n" };
  assert.deepEqual(editCall({ tool_call: { name: "apply_patch", input: patch } }), {
    tool_name: "apply_patch",
    tool_input: patch,
  });
  const read = { tool_call: { name: "read_files", input: { files: [{ path: "a.py" }, "b.py"] } } };
  assert.deepEqual(readPaths(read), ["a.py", "b.py"]);
});

test("a file edit carries that file's memories", async () => {
  const tmp = tmpDir();
  const block = JSON.stringify({ hookSpecificOutput: { additionalContext: "FILE RULES" } });
  const { run, calls } = fakeRun({ "loom-file-context.mjs": block });
  // the task is primed: the edit returns its file block alone
  await handlePrimed("PreToolUse", { ...ROOT, preToolUse: { toolName: "read_files" } }, { tmp, run, repo: noRepo });
  const edit = { ...ROOT, preToolUse: { toolName: "editor" }, tool_call: { input: { path: "/w/src/a.py" } } };
  assert.equal(await handle("PreToolUse", edit, { tmp, run, repo: noRepo }), "FILE RULES");
  const fc = calls.find((c) => c.script === "loom-file-context.mjs");
  assert.equal(fc.payload.tool_name, "Edit");
  assert.deepEqual(fc.payload.tool_input, { path: "/w/src/a.py" });
});

test("a file read brings the memories of its first files, before any edit", async () => {
  const tmp = tmpDir();
  const block = JSON.stringify({ hookSpecificOutput: { additionalContext: "READ RULES" } });
  const { run, calls } = fakeRun({ "loom-file-context.mjs": block });
  // the task is primed: the read returns its file blocks alone
  await handlePrimed("PreToolUse", { ...ROOT, preToolUse: { toolName: "submit" } }, { tmp, run, repo: noRepo });
  const files = JSON.stringify([
    { path: "./a.py" },
    { path: "./b.py" },
    { path: "./a.py" },
    { path: "c" },
    { path: "d" },
  ]);
  const read = { ...ROOT, preToolUse: { toolName: "read_files", parameters: { files } } };
  assert.equal(await handle("PreToolUse", read, { tmp, run, repo: noRepo }), "READ RULES\n\nREAD RULES\n\nREAD RULES");
  const asked = calls.filter((c) => c.script === "loom-file-context.mjs").map((c) => c.payload.tool_input.file_path);
  assert.deepEqual(asked, ["a.py", "b.py", "c"]);
  assert.deepEqual(readPaths({ preToolUse: { toolName: "read_files", parameters: { files: "not json" } } }), []);
  assert.deepEqual(readPaths({ preToolUse: { toolName: "editor" } }), []);
  const dup = JSON.stringify([{ path: "./a.py" }, { path: "a.py" }, { path: "b.py" }, { path: "c.py" }]);
  assert.deepEqual(readPaths({ preToolUse: { toolName: "read_files", parameters: { files: dup } } }), [
    "a.py",
    "b.py",
    "c.py",
  ]);
});

test("the primer finishes before any file lookup starts, and lookups run one at a time", async () => {
  const tmp = tmpDir();
  const log = [];
  const run = async (script) => {
    log.push(`start ${script}`);
    await new Promise((r) => setTimeout(r, 20));
    log.push(`end ${script}`);
    return "";
  };
  const files = JSON.stringify([{ path: "a.py" }, { path: "b.py" }]);
  await handle(
    "PreToolUse",
    { ...ROOT, preToolUse: { toolName: "read_files", parameters: { files } } },
    { tmp, run, repo: noRepo },
  );
  assert.deepEqual(log, [
    "start loom-session-start.mjs",
    "end loom-session-start.mjs",
    "start loom-file-context.mjs",
    "end loom-file-context.mjs",
    "start loom-file-context.mjs",
    "end loom-file-context.mjs",
  ]);
});

test("a read of two files marks both shown in the plugin's file-context state", async () => {
  const tmp = tmpDir();
  const saved = { ...process.env };
  for (const k of Object.keys(process.env)) {
    if (k.startsWith("LOOM_") || k.startsWith("CLAUDE_PLUGIN_OPTION_")) delete process.env[k];
  }
  Object.assign(process.env, { HOME: tmp, TMPDIR: tmp, LOOM_BASE_URL: "http://127.0.0.1:9", LOOM_API_KEY: "x" });
  try {
    const files = JSON.stringify([{ path: "a.py" }, { path: "b.py" }]);
    const read = { ...ROOT, taskId: "conv_two", preToolUse: { toolName: "read_files", parameters: { files } } };
    await handle("PreToolUse", read, { tmp });
    const state = readSessionState("file-context", "conv_two", { tmp });
    const shown = JSON.stringify(state);
    assert.ok(shown.includes("a.py") && shown.includes("b.py"), shown);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("with prompt recall off the first prompt is not kept", async () => {
  const tmp = tmpDir();
  process.env.LOOM_PROMPT_RECALL = "off";
  try {
    await handle("UserPromptSubmit", { ...ROOT, userPromptSubmit: { prompt: "fix the cache" } }, { tmp });
  } finally {
    delete process.env.LOOM_PROMPT_RECALL;
  }
  assert.equal(readSessionState("cline", ROOT.taskId, { tmp }).prompt, undefined);
});

test("the stored prompt is redacted before it is cut, so a secret across the cut is caught", async () => {
  const tmp = tmpDir();
  // an AWS-key-shaped token, built at runtime so the repository's secret scan has nothing to find
  const prompt = `${"x ".repeat(995)}${"AKIA"}ABCDEFGHIJKLMNOP and more`;
  await handle("UserPromptSubmit", { ...ROOT, userPromptSubmit: { prompt } }, { tmp, run: fakeRun({}).run });
  const kept = readSessionState("cline", ROOT.taskId, { tmp }).prompt;
  assert.ok(kept.length <= 2000);
  assert.ok(!kept.includes(`${"AKIA"}ABCDEF`), kept.slice(1980));
});

test("the repository cache is refreshed before file lookups only", async () => {
  const tmp = tmpDir();
  const asked = [];
  const repo = async (cwd) => asked.push(cwd);
  const { run } = fakeRun({});
  await handle("PreToolUse", { ...ROOT, preToolUse: { toolName: "submit" } }, { tmp, run, repo });
  assert.deepEqual(asked, []);
  const edit = { ...ROOT, preToolUse: { toolName: "editor" }, tool_call: { input: { path: "/w/a.py" } } };
  await handle("PreToolUse", edit, { tmp, run, repo });
  assert.deepEqual(asked, ["/w"]);
});

test("no file lookup starts once the hook has run its file budget", async () => {
  const tmp = tmpDir();
  const looked = [];
  let clock = 0;
  const now = () => clock;
  const run = async (script, _event, payload) => {
    if (script === "loom-file-context.mjs") looked.push(payload.tool_input.file_path);
    clock += 30; // each child takes 30 ms of the hook's time
    return "";
  };
  const files = JSON.stringify([{ path: "a.py" }, { path: "b.py" }, { path: "c.py" }]);
  const read = { ...ROOT, preToolUse: { toolName: "read_files", parameters: { files } } };
  await handle("PreToolUse", read, { tmp, run, repo: noRepo, filesMs: 50, now });
  // primer 0-30, a.py starts at 30 and ends at 60: b.py would start past the budget
  assert.deepEqual(looked, ["a.py"]);
});

test("with no token the adapter sends nothing, signs no machine up and caches no lookup", async () => {
  const tmp = tmpDir();
  const work = path.join(tmp, "work");
  fs.mkdirSync(work);
  spawnSync("git", ["init", "-q"], { cwd: work });
  spawnSync("git", ["remote", "add", "origin", "https://github.com/acme/widgets.git"], { cwd: work });
  let requests = 0;
  const server = http.createServer((_req, res) => {
    requests += 1;
    res.end("{}");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    const files = JSON.stringify([{ path: "a.py" }]);
    const payload = {
      taskId: "conv_none",
      workspaceInfo: { rootPath: work },
      preToolUse: { toolName: "read_files", parameters: { files } },
    };
    const child = spawn(process.execPath, [path.join(HERE, "..", "loom-cline.mjs"), "PreToolUse"], {
      env: {
        PATH: process.env.PATH,
        HOME: tmp,
        TMPDIR: tmp,
        LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
      },
    });
    let out = "";
    child.stdout.on("data", (c) => {
      out += c;
    });
    child.stdin.end(JSON.stringify(payload));
    const killer = setTimeout(() => child.kill("SIGKILL"), 30000); // a hang fails, not stalls
    const code = await new Promise((r) => child.on("close", (c) => r(c)));
    clearTimeout(killer);
    assert.equal(code, 0);
    const answer = JSON.parse(out);
    assert.doesNotMatch(String(answer.contextModification || ""), /sign-up|signup/i);
    assert.equal(requests, 0);
    assert.ok(!fs.existsSync(path.join(tmp, ".loom", "config.json")));
    assert.deepEqual(
      fs.readdirSync(tmp).filter((f) => f.startsWith("loom-shunt-")),
      [],
    );
  } finally {
    server.close();
  }
});

test("an event script runs the adapter through loom-node and keeps the prompt", () => {
  const dir = path.join(HERE, "..");
  const tmp = tmpDir();
  const r = spawnSync("/bin/sh", [path.join(dir, "hooks", "UserPromptSubmit")], {
    input: JSON.stringify({ ...ROOT, taskId: "conv_hook", userPromptSubmit: { prompt: "fix the cache" } }),
    env: { PATH: "/usr/bin:/bin", LOOM_CLINE_DIR: dir, LOOM_NODE: process.execPath, HOME: tmp, TMPDIR: tmp },
    encoding: "utf8",
    timeout: 30000,
  });
  assert.equal(r.status, 0);
  assert.deepEqual(JSON.parse(r.stdout), { cancel: false });
  assert.equal(readSessionState("cline", "conv_hook", { tmp }).prompt, "fix the cache");
});

test("the task is marked primed only once its answer is written", async () => {
  const tmp = tmpDir();
  const { run } = fakeRun({ "loom-session-start.mjs": "PRIMER" });
  const first = await handleRaw("PreToolUse", { ...ROOT, preToolUse: { toolName: "submit" } }, { tmp, run });
  assert.equal(first.context, "PRIMER");
  assert.equal(readSessionState("cline", ROOT.taskId, { tmp }).primed, undefined);
  first.prime();
  assert.equal(readSessionState("cline", ROOT.taskId, { tmp }).primed, true);
});

test("an event script runs through a LOOM_CLINE_DIR that is a symlink", () => {
  const tmp = tmpDir();
  const link = path.join(tmp, "cline-link");
  fs.symlinkSync(path.join(HERE, ".."), link);
  const r = spawnSync("/bin/sh", [path.join(link, "hooks", "UserPromptSubmit")], {
    input: JSON.stringify({ ...ROOT, taskId: "conv_link", userPromptSubmit: { prompt: "fix the cache" } }),
    env: { PATH: "/usr/bin:/bin", LOOM_CLINE_DIR: link, LOOM_NODE: process.execPath, HOME: tmp, TMPDIR: tmp },
    encoding: "utf8",
    timeout: 30000,
  });
  assert.equal(r.status, 0);
  assert.equal(readSessionState("cline", "conv_link", { tmp }).prompt, "fix the cache");
});

test("with LOOM_FILE_CONTEXT off a read neither looks files up nor refreshes the cache", async () => {
  const tmp = tmpDir();
  const asked = [];
  const { run, calls } = fakeRun({});
  const saved = process.env.LOOM_FILE_CONTEXT;
  process.env.LOOM_FILE_CONTEXT = "off";
  try {
    const files = JSON.stringify([{ path: "a.py" }]);
    const read = { ...ROOT, preToolUse: { toolName: "read_files", parameters: { files } } };
    await handle("PreToolUse", read, { tmp, run, repo: async (cwd) => asked.push(cwd) });
  } finally {
    if (saved === undefined) delete process.env.LOOM_FILE_CONTEXT;
    else process.env.LOOM_FILE_CONTEXT = saved;
  }
  assert.deepEqual(asked, []);
  assert.ok(!calls.some((c) => c.script === "loom-file-context.mjs"));
});

test("no task id, TaskStart and an empty script answer add nothing", async () => {
  const tmp = tmpDir();
  const { run } = fakeRun({});
  assert.equal(await handle("PreToolUse", { preToolUse: { toolName: "editor" } }, { tmp, run, repo: noRepo }), "");
  assert.equal(await handle("TaskStart", ROOT, { tmp, run, repo: noRepo }), "");
  assert.equal(await handle("PreToolUse", ROOT, { tmp, run, repo: noRepo }), "");
});

test("each event script answers without a Loom directory, and always exits 0", () => {
  for (const ev of ["UserPromptSubmit", "PreToolUse"]) {
    const r = spawnSync("sh", [path.join(HERE, "..", "hooks", ev)], {
      input: JSON.stringify(ROOT),
      env: { PATH: process.env.PATH },
      encoding: "utf8",
    });
    assert.equal(r.status, 0, ev);
    assert.deepEqual(JSON.parse(r.stdout), { cancel: false }, ev);
  }
});

test("against an unreachable server the adapter adds only the primer's static notes", () => {
  const tmp = tmpDir();
  const files = JSON.stringify([{ path: "./a.py" }]);
  const r = spawnSync(process.execPath, [path.join(HERE, "..", "loom-cline.mjs"), "PreToolUse"], {
    input: JSON.stringify({ ...ROOT, preToolUse: { toolName: "read_files", parameters: { files } } }),
    env: { PATH: process.env.PATH, HOME: tmp, LOOM_BASE_URL: "http://127.0.0.1:9", LOOM_API_KEY: "x", TMPDIR: tmp },
    encoding: "utf8",
    timeout: 30000,
  });
  assert.equal(r.status, 0);
  const answer = JSON.parse(r.stdout);
  assert.equal(answer.cancel, false);
  // the real primer ran and printed only its static notes: no memory, no recall
  assert.match(answer.contextModification, /^# Loom code index: /);
  assert.doesNotMatch(answer.contextModification, /loom-recalled-data/);
  assert.doesNotMatch(answer.contextModification, /loom_conversation_append/);
  // main marked the task primed once it wrote the answer: the next tool call has no primer
  assert.equal(readSessionState("cline", ROOT.taskId, { tmp }).primed, true);
  const again = spawnSync(process.execPath, [path.join(HERE, "..", "loom-cline.mjs"), "PreToolUse"], {
    input: JSON.stringify({ ...ROOT, preToolUse: { toolName: "submit" } }),
    env: { PATH: process.env.PATH, HOME: tmp, LOOM_BASE_URL: "http://127.0.0.1:9", LOOM_API_KEY: "x", TMPDIR: tmp },
    encoding: "utf8",
    timeout: 30000,
  });
  assert.deepEqual(JSON.parse(again.stdout), { cancel: false });
});

test("an event script answers and exits 0 when loom-node finds no Node 18+", () => {
  const dir = path.join(HERE, "..");
  const tmp = tmpDir();
  const r = spawnSync("/bin/sh", [path.join(dir, "hooks", "PreToolUse")], {
    input: JSON.stringify(ROOT),
    env: { PATH: "/usr/bin:/bin", LOOM_CLINE_DIR: dir, LOOM_NODE: "/nonexistent/node", HOME: tmp, TMPDIR: tmp },
    encoding: "utf8",
    timeout: 30000,
  });
  assert.equal(r.status, 0);
  assert.deepEqual(JSON.parse(r.stdout), { cancel: false });
});
