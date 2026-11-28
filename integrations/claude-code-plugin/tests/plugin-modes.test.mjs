// The plugin's three modes. Every hook script names the plane its work
// belongs to, and a session in one mode sends that plane's ops and the
// ledger's, nothing else.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { MODES, PLANES, planeEnabled, pluginMode } from "../lib/loom-client.mjs";
import { runSession } from "./mode-session.mjs";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const hookScripts = fs
  .readdirSync(path.join(root, "hooks"))
  .filter((f) => /^loom-[a-z-]+\.mjs$/.test(f))
  .map((f) => f.replace(/\.mjs$/, ""));

/** The planes a hook script names in its `planeEnabled("<plane>")` checks. */
function declared(script) {
  const src = fs.readFileSync(path.join(root, "hooks", `${script}.mjs`), "utf8");
  return [...new Set([...src.matchAll(/planeEnabled\("([a-z]+)"\)/g)].map((m) => m[1]))].sort();
}

test("the mode is the option, else LOOM_MODE, else both; a plane runs in its mode and in both", () => {
  assert.deepEqual(MODES, ["both", "memory", "code"]);
  assert.equal(pluginMode({}), "both");
  assert.equal(pluginMode({ CLAUDE_PLUGIN_OPTION_MODE: " Code " }), "code");
  assert.equal(pluginMode({ LOOM_MODE: "memory" }), "memory");
  // The option also picked the MCP listing, so it outranks the variable.
  assert.equal(pluginMode({ CLAUDE_PLUGIN_OPTION_MODE: "memory", LOOM_MODE: "code" }), "memory");
  // A value that is no mode is the default, as the server reads an unknown `?mode=`.
  for (const v of ["", "all", "index", "${user_config.mode}"]) assert.equal(pluginMode({ LOOM_MODE: v }), "both", v);
  const runs = (mode) => PLANES.filter((plane) => planeEnabled(plane, { LOOM_MODE: mode }));
  assert.deepEqual(runs("both"), ["memory", "code", "ledger"]);
  assert.deepEqual(runs("memory"), ["memory", "ledger"]);
  assert.deepEqual(runs("code"), ["code", "ledger"]);
  assert.throws(() => planeEnabled("tracker", {}), /unknown plane/);
});

test("every hook script names its plane, and HOOKS.md lists the same planes for it", () => {
  assert.ok(hookScripts.length >= 19, `found only ${hookScripts.length} hook scripts; the scan is broken`);
  const doc = fs.readFileSync(path.join(root, "hooks", "HOOKS.md"), "utf8");
  const section = doc.slice(doc.indexOf("\n## Modes\n")).split(/\n## /)[1] || "";
  const listed = {};
  for (const row of section.matchAll(/^\| (loom-[a-z-]+) +\| ([^|]+)\|/gm)) {
    listed[row[1]] = [...new Set(row[2].match(/\b(?:memory|code|ledger)\b/g) || [])].sort();
  }
  for (const script of hookScripts) {
    const planes = declared(script);
    assert.ok(planes.length, `${script} names no plane: gate it with planeEnabled("memory" | "code" | "ledger")`);
    for (const plane of planes) assert.ok(PLANES.includes(plane), `${script}: ${plane} is no plane`);
    assert.deepEqual(listed[script], planes, `${script}: the Modes table in HOOKS.md and the script disagree`);
  }
  assert.deepEqual(Object.keys(listed).sort(), [...hookScripts].sort(), "the Modes table lists a script that is gone");
});

// What the scripted session sends, by plane.
const CODE = ["code.impact", "code.index_status", "code.outline", "code.repos", "code.search", "code.test_gate"];
const LEDGER = ["cost.record_turns", "run.begin", "run.complete"];
const MEMORY = [
  "collection.list",
  "context.pack",
  "conversation.append",
  "memory.for_file",
  "memory.set_from_messages",
  "namespace.audit",
  "observe.crystallize",
  "observe.record",
  "session.brief_save",
  "task.counts",
  "working_memory.set",
];

test("a session sends its mode's ops and no other: prompt, read, grep, edit, test run, stop, session end", async () => {
  const [both, unset, memory, code] = await Promise.all(
    ["both", undefined, "memory", "code"].map((mode) => runSession({ mode })),
  );
  assert.deepEqual(both.ops, [...CODE, ...LEDGER, ...MEMORY].sort());
  assert.deepEqual(unset.ops, both.ops, "the default is both");
  // Index only: nothing of the session is captured, and nothing is handed to the server to extract.
  assert.deepEqual(code.ops, [...CODE, ...LEDGER].sort());
  // Memory only: no code request.
  assert.deepEqual(memory.ops, [...LEDGER, ...MEMORY].sort());
  // What the hooks add to the first request is smaller in each one-plane mode.
  const first = (s) => Buffer.byteLength(s.startText) + Buffer.byteLength(s.promptContext);
  assert.ok(first(memory) < first(both), `memory ${first(memory)} >= both ${first(both)}`);
  assert.ok(first(code) < first(both), `code ${first(code)} >= both ${first(both)}`);
  assert.match(code.startText, /^# Loom code index: example\/app @ main[^\n]*loom_code_\* calls\.\n$/);
  assert.match(memory.startText, /^# Loom memory primer: Save memory about this repository[^\n]*\n$/);
});

test("the other wirings name their mode in LOOM_MODE", async () => {
  const code = await runSession({ env: { LOOM_MODE: "code" } });
  assert.deepEqual(code.ops, [...CODE, ...LEDGER].sort());
});
