// Per-session tmp files are owner-only, refuse planted paths, and the
// session's journal and code-trace stash are gone after SessionEnd.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { writeJsonFile, writePrivateFile } from "../lib/tmp-state.mjs";
import { appendTurn, journalPath, readJournal } from "../lib/turn-journal.mjs";

const posix = process.platform !== "win32";
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "loom-private-test-"));
const modeOf = (file) => fs.statSync(file).mode & 0o777;

test("the turn journal is created 0600, whatever the umask", { skip: !posix }, () => {
  const dir = tmp();
  const old = process.umask(0o022);
  try {
    assert.ok(appendTurn("s1", "user", "my secret plan", { dir }));
  } finally {
    process.umask(old);
  }
  assert.equal(modeOf(journalPath("s1", dir)), 0o600);
  assert.equal(readJournal("s1", { dir })[0].content, "my secret plan");
});

test("a state file left world-readable by an older version is tightened", { skip: !posix }, () => {
  const file = path.join(tmp(), "loom-x.json");
  fs.writeFileSync(file, "{}", { mode: 0o644 });
  fs.chmodSync(file, 0o644);
  assert.ok(writeJsonFile(file, { a: 1 }));
  assert.equal(modeOf(file), 0o600);
});

test("a planted symlink is refused, and its target is untouched", { skip: !posix }, () => {
  const dir = tmp();
  const target = path.join(dir, "victim.txt");
  fs.writeFileSync(target, "original");
  fs.symlinkSync(target, journalPath("s2", dir));
  assert.equal(appendTurn("s2", "user", "leak me", { dir }), false);
  assert.throws(() => writePrivateFile(journalPath("s2", dir), "x"));
  assert.equal(fs.readFileSync(target, "utf8"), "original");
});

test("a planted FIFO fails the write instead of hanging the hook", { skip: !posix }, () => {
  const fifo = path.join(tmp(), "loom-fifo");
  if (spawnSync("mkfifo", [fifo]).status !== 0) return;
  assert.equal(writeJsonFile(fifo, { a: 1 }), false);
});

test("SessionEnd removes the turn journal and the code-trace stash", { skip: !posix }, () => {
  const dir = tmp();
  const sid = "s-end";
  assert.ok(appendTurn(sid, "user", "hello", { dir }));
  const stash = path.join(dir, `loom-code-trace-${sid}.json`);
  fs.writeFileSync(stash, "[]");
  const env = {
    PATH: process.env.PATH,
    HOME: dir,
    TMPDIR: dir,
    LOOM_BASE_URL: "http://127.0.0.1:9",
    LOOM_API_KEY: "x.y.z",
    LOOM_SESSION_MEMORY: "off",
  };
  for (const name of ["loom-capture.mjs", "loom-session-memory.mjs"]) {
    const hook = fileURLToPath(new URL(`../hooks/${name}`, import.meta.url));
    spawnSync(process.execPath, [hook, "SessionEnd"], {
      input: JSON.stringify({ session_id: sid, cwd: dir }),
      env,
      timeout: 10_000,
    });
  }
  assert.equal(fs.existsSync(journalPath(sid, dir)), false, "journal removed");
  assert.equal(fs.existsSync(stash), false, "code-trace stash removed");
});
