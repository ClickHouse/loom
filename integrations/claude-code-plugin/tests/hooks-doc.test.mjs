// HOOKS.md is the lifecycle doc: every wired script and every setting the
// capture-lane scripts read must appear in it.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const doc = fs.readFileSync(path.join(root, "hooks", "HOOKS.md"), "utf8");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "hooks", "hooks.json"), "utf8"));

// The code-plane scripts document their own settings.
const CODE_PLANE = /code-shunt|test-gate|blast-radius|file-context|validate-gate|repo-scope/;
// Test seams, not settings.
const NOT_SETTINGS = new Set(["LOOM_HOOK_NO_MAIN"]);

test("every script hooks.json wires is in the events table", () => {
  for (const groups of Object.values(manifest.hooks)) {
    for (const g of groups) {
      for (const h of g.hooks) {
        const script = h.command.match(/hooks\/(loom-[a-z-]+)\.mjs/)[1];
        assert.match(doc, new RegExp(`\\|\\s*${script}\\s*\\|`), `${script} is missing from the events table`);
      }
    }
  }
});

test("every setting the capture-lane scripts read is documented", () => {
  const read = new Set();
  for (const dir of ["hooks", "lib", "bin"]) {
    for (const f of fs.readdirSync(path.join(root, dir))) {
      if (!f.endsWith(".mjs") || CODE_PLANE.test(f)) continue;
      const src = fs.readFileSync(path.join(root, dir, f), "utf8");
      const re = /(?:env\.|env\[\s*"|envSwitch\(\s*"|envNumber\(\s*")((?:LOOM|CLAUDE_PLUGIN_OPTION)_[A-Z0-9_]+)/g;
      for (const m of src.matchAll(re)) if (!NOT_SETTINGS.has(m[1])) read.add(m[1]);
    }
  }
  assert.ok(read.size > 30, `found only ${read.size} settings; the scan is broken`);
  const missing = [...read].filter((name) => {
    if (doc.includes(name)) return false;
    const option = name.match(/^CLAUDE_PLUGIN_OPTION_(.+)$/);
    return !(option && doc.includes(`\`${option[1].toLowerCase()}\``));
  });
  assert.deepEqual(missing.sort(), []);
});
