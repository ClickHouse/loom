import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { git, gitToplevel, realPath } from "../lib/loom-client.mjs";

const tmpDir = () => fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-git-")));

test("git: a diff over the default 1 MB child buffer still arrives whole and untrimmed", () => {
  const dir = tmpDir();
  const g = (...a) => execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", ...a], { cwd: dir });
  fs.writeFileSync(path.join(dir, "a.txt"), "x\n");
  g("init", "-q");
  g("add", ".");
  g("commit", "-qm", "c");
  fs.writeFileSync(path.join(dir, "a.txt"), `${"y".repeat(100)}\n`.repeat(20000));
  const diff = git(["diff", "--unified=0", "HEAD"], dir, { timeoutMs: 5000, raw: true });
  assert.ok(diff.length > 1.5 * 1024 * 1024, `diff is ${diff.length} bytes`);
  assert.equal(diff.endsWith("\n"), true);
  assert.equal(gitToplevel(dir), realPath(dir));
  assert.equal(git(["no-such-command"], dir), "");
});
