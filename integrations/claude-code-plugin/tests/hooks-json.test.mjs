import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { LOOKUPS, RECORDERS } from "../hooks/loom-record.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(here, "..", "hooks", "hooks.json"), "utf8"));

/** The script names hooks.json wires under `event`. */
function wired(event) {
  return manifest.hooks[event].flatMap((g) => g.hooks).map((h) => h.args[0].match(/hooks\/(loom-[a-z-]+)\.mjs$/)[1]);
}

test("a script the launcher runs is never wired beside it", () => {
  // The launcher runs an event's recording scripts in one process (a
  // detached one, or its own at SessionEnd) and its lookups in its own; one
  // wired directly as well would run twice and hold the host for its request.
  for (const [event, scripts] of Object.entries(RECORDERS)) {
    const names = wired(event);
    const run = [...scripts, ...(LOOKUPS[event] || [])];
    assert.equal(names.filter((n) => n === "loom-record").length, 1, `${event}: the launcher is wired once`);
    assert.deepEqual(
      names.filter((n) => run.includes(n)),
      [],
      `${event}: wired beside the launcher`,
    );
  }
  assert.deepEqual(
    Object.keys(LOOKUPS).filter((event) => !Object.hasOwn(RECORDERS, event)),
    [],
    "a lookup's event is one the launcher is wired for",
  );
});

test("a prompt starts one process, which opens the session's run", () => {
  assert.deepEqual(wired("UserPromptSubmit"), ["loom-record"]);
  assert.ok(RECORDERS.UserPromptSubmit.includes("loom-run"));
});

test("the Stop gates are wired directly: the host reads what they print", () => {
  assert.deepEqual(wired("Stop"), ["loom-record", "loom-test-gate", "loom-validate-gate", "loom-namespace-health"]);
});

test("one hook names a changed file's tests per tool: the blast radius after an edit tool, the test gate after a shell", () => {
  // Both print additional context for the call; a tool that started both would get the tests line twice.
  const tools = (script) =>
    manifest.hooks.PostToolUse.filter((g) => g.hooks.some((h) => h.args[0].endsWith(`/hooks/${script}.mjs`))).flatMap(
      (g) => g.matcher.split("|"),
    );
  assert.deepEqual(tools("loom-blast-radius"), ["Edit", "Write", "MultiEdit"]);
  assert.deepEqual(tools("loom-test-gate"), ["Bash", "PowerShell"]);
  const gate = manifest.hooks.PostToolUse.flatMap((g) => g.hooks).find((h) => h.args[0].endsWith("loom-test-gate.mjs"));
  assert.equal(gate.args[1], "PostToolUse", "the script reads its event from this argument");
});

test("the file context is wired after a read, before an edit and at a compaction", () => {
  // After a read it adds the file's memory to the tool result, and a call the
  // shunt refused never gets there; a compaction drops the blocks, so the
  // script forgets which files it showed.
  const groups = (event) =>
    manifest.hooks[event].filter((g) => g.hooks.some((h) => h.args[0].endsWith("/hooks/loom-file-context.mjs")));
  assert.deepEqual(
    groups("PostToolUse").map((g) => g.matcher),
    ["Read|Grep|Bash"],
  );
  const shunt = manifest.hooks.PreToolUse.find((g) => g.hooks.some((h) => h.args[0].endsWith("loom-code-shunt.mjs")));
  assert.equal(groups("PostToolUse")[0].matcher, shunt.matcher, "the reads the shunt judges");
  assert.deepEqual(
    groups("PreToolUse").map((g) => g.matcher),
    ["Edit|Write|MultiEdit|NotebookEdit"],
  );
  assert.equal(groups("PreCompact").length, 1);
  for (const event of ["PostToolUse", "PreToolUse", "PreCompact"]) {
    const h = groups(event)[0].hooks.find((x) => x.args[0].endsWith("loom-file-context.mjs"));
    assert.equal(h.args[1], event, "the script reads its event from this argument");
  }
});

test("the SessionEnd hook declares a timeout: Claude Code gives the event 1.5 s by default", () => {
  // The launcher runs the event's five recording scripts in its own process
  // and waits for them: a slow server or a loaded machine passes the default
  // budget, and a launcher killed there leaves their requests unsent. A
  // `timeout` raises the event budget to match (docs: hooks-guide,
  // limitations), up to 60 s.
  const hooks = manifest.hooks.SessionEnd.flatMap((g) => g.hooks);
  assert.equal(hooks.length, 1);
  for (const h of hooks) {
    assert.ok(Number.isInteger(h.timeout) && h.timeout >= 5 && h.timeout <= 60, `${h.args[0]}: timeout=${h.timeout}`);
  }
});

test("the Codex hooks.json carries only the fields Codex's strict parser accepts", () => {
  // Codex 0.154: `failed to parse plugin hooks config …/hooks.json: unknown
  // field \`_comment\`, expected \`description\` or \`hooks\`` — and with that
  // one error NO hook loads. Claude Code
  // only warns; Codex refuses the file.
  const codex = JSON.parse(fs.readFileSync(path.join(here, "..", "..", "codex-plugin", "hooks", "hooks.json"), "utf8"));
  assert.deepEqual(
    Object.keys(codex).filter((k) => !["description", "hooks"].includes(k)),
    [],
  );
  for (const [event, groups] of Object.entries(codex.hooks)) {
    for (const g of groups) {
      assert.deepEqual(
        Object.keys(g).filter((k) => !["hooks", "matcher"].includes(k)),
        [],
        event,
      );
      for (const h of g.hooks) {
        assert.deepEqual(
          // commandWindows: what Codex 0.131+ runs on Windows, through cmd.exe;
          // earlier builds ignore an unknown handler key (only top-level keys are strict)
          Object.keys(h).filter((k) => !["type", "command", "commandWindows", "timeout", "statusMessage"].includes(k)),
          [],
          `${event}: ${h.command}`,
        );
        assert.match(h.command, / codex$/, "every Codex hook names its client as the third argument");
      }
    }
  }
});
