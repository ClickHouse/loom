// The hook scripts serve more than one coding agent. The
// client is named by argv, shell tools fold onto "Bash", argv-array commands
// read as text, apply_patch names its files, and the run-id prefix follows
// the client.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  commandText,
  extractFiles,
  hookClient,
  inferActionType,
  normalizeToolName,
  patchFiles,
  runPrefixFor,
} from "../lib/loom-client.mjs";
import { runIdFor, beginBody } from "../lib/run-hook.mjs";
import { targetPath } from "../lib/file-context.mjs";
import { sessionMemoryBody } from "../lib/session-memory.mjs";

const PATCH = [
  "*** Begin Patch",
  "*** Update File: src/a.py",
  "@@",
  "-x = 1",
  "+x = 2",
  "*** Add File: src/b.py",
  "+print(1)",
  "*** Delete File: old.txt",
  "*** End Patch",
].join("\n");

test("hookClient: argv[3] names the client, then LOOM_CLIENT, then Claude Code", () => {
  assert.equal(hookClient(["node", "x.mjs", "Stop", "codex"], {}), "codex");
  assert.equal(hookClient(["node", "x.mjs", "Stop"], { LOOM_CLIENT: "codex" }), "codex");
  assert.equal(hookClient(["node", "x.mjs", "Stop"], {}), "claude-code");
  // a malformed name never becomes an agent label
  assert.equal(hookClient(["node", "x.mjs", "Stop", "Not A Client!"], { LOOM_CLIENT: "" }), "claude-code");
  assert.equal(hookClient(["node", "x.mjs", "Stop", "CODEX"], {}), "codex");
});

test("runPrefixFor / runIdFor: cc- for Claude Code and unknowns, cx- for Codex", () => {
  assert.equal(runPrefixFor("claude-code"), "cc");
  assert.equal(runPrefixFor("codex"), "cx");
  assert.equal(runPrefixFor("something-else"), "cc");
  assert.equal(runIdFor("s1", "codex"), "cx-s1");
  assert.equal(runIdFor("s1", "claude-code"), "cc-s1");
  assert.equal(runIdFor("", "codex"), "");
});

test("normalizeToolName: shell tools fold onto Bash; everything else is itself", () => {
  for (const n of ["shell", "exec_command", "local_shell", "container.exec", "shell_command"]) {
    assert.equal(normalizeToolName(n), "Bash");
  }
  assert.equal(normalizeToolName("Bash"), "Bash");
  assert.equal(normalizeToolName("apply_patch"), "apply_patch");
  assert.equal(normalizeToolName("Read"), "Read");
  assert.equal(normalizeToolName(undefined), "");
});

test("commandText: string as is; argv joined; a sh -c wrapper stripped", () => {
  assert.equal(commandText({ command: "cat x" }), "cat x");
  assert.equal(commandText({ command: ["bash", "-lc", "rg foo src"] }), "rg foo src");
  assert.equal(commandText({ command: ["/bin/sh", "-c", "cat", "a.py"] }), "cat a.py");
  assert.equal(commandText({ command: ["zsh", "-c", "ls"] }), "ls");
  assert.equal(commandText({ command: ["rg", "foo", "src"] }), "rg foo src");
  assert.equal(commandText({ cmd: "echo 1" }), "echo 1");
  assert.equal(commandText({}), "");
  assert.equal(commandText(null), "");
});

test("patchFiles / extractFiles: apply_patch names its files", () => {
  assert.deepEqual(patchFiles(PATCH), ["src/a.py", "src/b.py", "old.txt"]);
  assert.deepEqual(patchFiles("no patch here"), []);
  assert.deepEqual(extractFiles({ input: PATCH }), ["src/a.py", "src/b.py", "old.txt"]);
  assert.deepEqual(extractFiles({ patch: PATCH }), ["src/a.py", "src/b.py", "old.txt"]);
  // a string field without the envelope is not scanned
  assert.deepEqual(extractFiles({ input: "*** Update File: x" }), []);
  // the Claude Code shapes are unchanged
  assert.deepEqual(extractFiles({ file_path: "a.py" }), ["a.py"]);
});

test("inferActionType: shell variants are command runs, apply_patch is a file edit", () => {
  assert.equal(inferActionType("shell", false), "command_run");
  assert.equal(inferActionType("exec_command", false), "command_run");
  assert.equal(inferActionType("apply_patch", false), "file_edit");
  assert.equal(inferActionType("Bash", false), "command_run");
  assert.equal(inferActionType("Read", false), "file_read");
  assert.equal(inferActionType("apply_patch", true), "error");
});

test("targetPath: apply_patch asks about the first file the patch names", () => {
  assert.equal(targetPath("apply_patch", { input: PATCH }), "src/a.py");
  assert.equal(targetPath("apply_patch", { patch: PATCH }), "src/a.py");
  assert.equal(targetPath("apply_patch", { input: "garbage" }), "");
  assert.equal(targetPath("Edit", { file_path: "/r/a" }), "/r/a");
});

test("beginBody / sessionMemoryBody carry the client the hook was told", () => {
  const scope = { org: "o", namespace: "n", agent: "codex", project: "p", tags: [] };
  const saved = process.argv;
  process.argv = ["node", "loom-run.mjs", "UserPromptSubmit", "codex"];
  try {
    const b = beginBody(scope, { session_id: "s9", cwd: "/w" }, "fix the build");
    assert.equal(b.run_id, "cx-s9");
    assert.equal(b.environment, "codex");
    assert.equal(b.consumer, "codex");
    const sm = sessionMemoryBody(scope, { session_id: "s9" }, [
      { role: "user", content: "fix the build" },
      { role: "assistant", content: "done" },
    ]);
    assert.equal(sm.run_id, "cx-s9");
    assert.ok(sm.extra_tags.includes("source:codex"));
  } finally {
    process.argv = saved;
  }
});
