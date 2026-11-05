import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(fs.readFileSync(path.join(here, "..", "hooks", "hooks.json"), "utf8"));

test("every SessionEnd hook declares a timeout: Claude Code gives the event 1.5 s shared by default", () => {
  // Five hooks post one row each at exit (the fifth, loom-session-brief,
  // hands its post to the detached flusher and exits); on the default budget
  // the last one is killed and reported as "Hook cancelled" after its row
  // already landed. A per-hook `timeout` raises the
  // event budget to match (docs: hooks-guide, limitations), up to 60 s.
  const groups = manifest.hooks.SessionEnd;
  const hooks = groups.flatMap((g) => g.hooks);
  assert.equal(hooks.length, 5);
  for (const h of hooks) {
    assert.ok(Number.isInteger(h.timeout) && h.timeout >= 5 && h.timeout <= 60, `${h.command}: timeout=${h.timeout}`);
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
          // commandWindows: what Codex runs on Windows, through cmd.exe
          Object.keys(h).filter((k) => !["type", "command", "commandWindows", "timeout"].includes(k)),
          [],
          `${event}: ${h.command}`,
        );
        assert.match(h.command, / codex$/, "every Codex hook names its client as the third argument");
      }
    }
  }
});
