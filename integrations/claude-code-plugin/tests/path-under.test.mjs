// A payload path and git's toplevel can name one directory two ways: macOS
// reaches /tmp through /private/tmp, Windows has 8.3 short names and git's
// C:/ form. pathUnder compares them as the file system names them.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  gitToplevel,
  pathUnder,
  realPath,
  resolveProject,
  resolveRelDir,
  shuntCachePath,
} from "../lib/loom-client.mjs";
import { sessionScope } from "../lib/test-gate.mjs";

const WINDOWS = process.platform === "win32";

function checkout() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-path-under-"));
  const repo = path.join(dir, "repo");
  fs.mkdirSync(path.join(repo, "src", "deep"), { recursive: true });
  execFileSync("git", ["init", "-q"], { cwd: repo });
  return { dir, repo };
}

test("pathUnder: inside, the root itself, outside, and a sibling with the root's prefix", () => {
  const { dir, repo } = checkout();
  try {
    assert.equal(pathUnder(repo, path.join(repo, "src", "deep")), "src/deep");
    assert.equal(pathUnder(repo, repo), "");
    assert.equal(pathUnder(repo, dir), null);
    assert.equal(pathUnder(repo, `${repo}-other`), null);
    assert.equal(pathUnder("", repo), null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("pathUnder: a file not written yet resolves under its existing parent", () => {
  const { dir, repo } = checkout();
  try {
    assert.equal(pathUnder(repo, path.join(repo, "src", "new", "a.py")), "src/new/a.py");
    assert.equal(realPath(path.join(repo, "nope")), path.join(realPath(repo), "nope"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a symlink inside the checkout keeps its own name, wherever it points", { skip: WINDOWS }, () => {
  const { dir, repo } = checkout();
  try {
    const outside = path.join(dir, "python3.12");
    fs.writeFileSync(outside, "");
    fs.mkdirSync(path.join(repo, ".venv", "bin"), { recursive: true });
    fs.symlinkSync(outside, path.join(repo, ".venv", "bin", "python"));
    assert.equal(pathUnder(repo, path.join(repo, ".venv", "bin", "python")), ".venv/bin/python");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a checkout reached through a symlink: git's toplevel and the cwd still agree", { skip: WINDOWS }, () => {
  const { dir, repo } = checkout();
  try {
    const link = path.join(dir, "link");
    fs.symlinkSync(repo, link);
    const cwd = path.join(link, "src", "deep");
    assert.equal(gitToplevel(cwd), realPath(repo));
    assert.equal(resolveRelDir(cwd), "src/deep");
    // The project is the folder git names: its spelling picks the namespace.
    const gitTop = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd, encoding: "utf8" }).trim();
    assert.equal(resolveProject(cwd), path.basename(gitTop));
    assert.equal(pathUnder(gitToplevel(cwd), path.join(link, "src", "a.py")), "src/a.py");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("the test gate sees a command that names a file through the symlinked path the session runs in", {
  skip: WINDOWS,
}, () => {
  const { dir, repo } = checkout();
  try {
    const link = path.join(dir, "link");
    fs.symlinkSync(repo, link);
    const cwd = path.join(link, "src");
    const said = [
      {
        type: "assistant",
        message: {
          content: [{ type: "tool_use", name: "Bash", input: { command: `sed -i s/a/b/ ${link}/src/deep/a.py` } }],
        },
      },
    ];
    assert.deepEqual(sessionScope(["src/deep/a.py"], said, { root: gitToplevel(cwd), cwd }), ["src/deep/a.py"]);
    // A session started at the link itself, the usual way to use one.
    assert.equal(pathUnder(gitToplevel(link), link), "");
    assert.deepEqual(sessionScope(["src/deep/a.py"], said, { root: gitToplevel(link), cwd: link }), ["src/deep/a.py"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("git's C:/ spelling of the toplevel matches the cwd's own", { skip: !WINDOWS }, () => {
  const { dir, repo } = checkout();
  try {
    const cwd = path.join(repo, "src", "deep");
    const forward = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd, encoding: "utf8" }).trim();
    assert.equal(pathUnder(forward, cwd), "src/deep");
    assert.equal(resolveRelDir(cwd), "src/deep");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a Windows 8.3 spelling of the checkout keys the same shunt cache entry", { skip: !WINDOWS }, (t) => {
  const { dir, repo } = checkout();
  try {
    // os.tmpdir() is often the short form (C:\Users\RUNNER~1\...); its long
    // form is what git and realpath name.
    const long = fs.realpathSync.native(repo);
    if (long === repo) return t.skip("the temp directory has no short name here");
    assert.equal(shuntCachePath(repo), shuntCachePath(long));
    assert.equal(shuntCachePath(long.replace(/\\/g, "/")), shuntCachePath(long));
    assert.equal(pathUnder(repo, path.join(long, "src", "deep")), "src/deep");
    const gitTop = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd: repo, encoding: "utf8" }).trim();
    assert.equal(resolveProject(repo), path.basename(gitTop));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
