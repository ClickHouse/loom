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
  // The launcher runs an event's recording scripts in one detached process
  // and its lookups in its own; one wired directly as well would run twice
  // and hold the host for its request.
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

test("a prompt starts one process", () => {
  assert.deepEqual(wired("UserPromptSubmit"), ["loom-record"]);
});

test("the Stop gates are wired directly: the host reads what they print", () => {
  assert.deepEqual(wired("Stop"), ["loom-record", "loom-test-gate", "loom-validate-gate", "loom-namespace-health"]);
});

test("the SessionEnd hook asks for no longer than the host's default budget", () => {
  // Claude Code gives the event 1.5 s by default. The launcher hands the
  // payload off and exits, so it needs no `timeout` of its own.
  const hooks = manifest.hooks.SessionEnd.flatMap((g) => g.hooks);
  assert.deepEqual(wired("SessionEnd"), ["loom-record"]);
  for (const h of hooks) assert.equal(h.timeout, undefined, h.args[0]);
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
