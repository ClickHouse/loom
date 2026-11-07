import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const skill = fs.readFileSync(path.join(here, "..", "skills", "loom-code", "SKILL.md"), "utf8");

test("the loom-code skill stays short and names the MCP tools; the tables live in reference.md", () => {
  // Codex loads a skill by reading SKILL.md whole (`sed -n 1,240p`): a
  // definition question can pay ~2,400 tokens for the file before the one
  // loom_code_symbol call. Claude Code pays it on
  // invocation too. Keep the short form short.
  assert.ok(skill.length <= 4200, `SKILL.md is ${skill.length} chars`);
  assert.ok(skill.split("\n").length <= 70, "SKILL.md is over 70 lines");
  for (const tool of [
    "loom_code_symbol",
    "loom_code_callers",
    "loom_code_search",
    "loom_code_grep",
    "loom_code_outline",
    "loom_code_enclosing",
    "loom_code_ask",
    "loom_code_context_pack",
  ]) {
    assert.ok(skill.includes(tool), `SKILL.md names ${tool}`);
  }
  assert.match(skill, /reference\.md/);
  assert.ok(fs.existsSync(path.join(here, "..", "skills", "loom-code", "reference.md")));
  assert.match(skill, /^---\nname: loom-code\n/, "frontmatter first");
});

test("every plugin skill's SKILL.md stays under the size a host reads whole; long ones carry a reference.md", () => {
  // Codex loads a skill by reading SKILL.md in full: a long loom-mentor skill
  // costs ~4,000 tokens on a single duplicate-check question.
  const caps = {
    "loom-code": 4200,
    "loom-mentor": 4800,
    "loom-memory": 5200,
    "loom-status": 4200,
    "improve-loom-for-my-repo": 4200,
    "loom-optimize": 9000,
    "loom-session": 4200,
  };
  for (const [name, cap] of Object.entries(caps)) {
    const dir = path.join(here, "..", "skills", name);
    const text = fs.readFileSync(path.join(dir, "SKILL.md"), "utf8");
    assert.ok(text.length <= cap, `${name}/SKILL.md is ${text.length} chars (cap ${cap})`);
    assert.match(text, /^---\nname: /, `${name}: frontmatter first`);
    if (["loom-code", "loom-mentor", "loom-memory"].includes(name)) {
      assert.ok(fs.existsSync(path.join(dir, "reference.md")), `${name} has a reference.md`);
      assert.match(text, /reference\.md/, `${name} points at its reference`);
    }
  }
  const mentor = fs.readFileSync(path.join(here, "..", "skills", "loom-mentor", "SKILL.md"), "utf8");
  for (const tool of [
    "loom_tracker_duplicate_check",
    "loom_tracker_similar",
    "loom_code_bisect_candidates",
    "loom_code_pr_check",
  ]) {
    assert.ok(mentor.includes(tool), `mentor names ${tool}`);
  }
  assert.match(mentor, /toolset=tracker/);
});
