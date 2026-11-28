import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { RECORDERS } from "../hooks/loom-record.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(here, "..", "hooks", "hooks.json"), "utf8"));

/** The script names hooks.json wires under `event`. */
function wired(event) {
  return manifest.hooks[event].flatMap((g) => g.hooks).map((h) => h.args[0].match(/hooks\/(loom-[a-z-]+)\.mjs$/)[1]);
}

test("a recording script runs through the launcher, never beside it", () => {
  // The launcher runs these scripts in one process (a detached one, or its
  // own at SessionEnd); one wired directly as well would record twice and
  // hold the host for its request.
  for (const [event, scripts] of Object.entries(RECORDERS)) {
    const names = wired(event);
    assert.equal(names.filter((n) => n === "loom-record").length, 1, `${event}: the launcher is wired once`);
    assert.deepEqual(
      names.filter((n) => scripts.includes(n)),
      [],
      `${event}: wired beside the launcher`,
    );
  }
});

test("the Stop gates are wired directly: the host reads what they print", () => {
  assert.deepEqual(wired("Stop"), ["loom-record", "loom-test-gate", "loom-validate-gate", "loom-namespace-health"]);
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
