// Per-session tmp files are owner-only, refuse planted paths on read and write,
// SessionEnd leaves the journal its parallel hooks read, and SessionStart sweeps
// stale state.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  readJsonFile,
  readPrivateFile,
  sweepStaleStateFiles,
  writeJsonFile,
  writePrivateFile,
} from "../lib/tmp-state.mjs";
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

test("an existing world-readable state file is tightened", { skip: !posix }, () => {
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

test("a planted hard link to a file of ours is refused before it is truncated", { skip: !posix }, () => {
  const dir = tmp();
  const target = path.join(dir, "victim.txt");
  fs.writeFileSync(target, "original");
  fs.linkSync(target, path.join(dir, "loom-x.json"));
  assert.equal(writeJsonFile(path.join(dir, "loom-x.json"), { a: 1 }), false);
  assert.throws(() => writePrivateFile(path.join(dir, "loom-x.json"), "x", { append: true }));
  assert.equal(fs.readFileSync(target, "utf8"), "original");
});

test("a planted hard link is not read as state", { skip: !posix }, () => {
  const dir = tmp();
  const target = path.join(dir, "victim.json");
  fs.writeFileSync(target, '{"secret":1}');
  fs.linkSync(target, path.join(dir, "loom-x.json"));
  assert.throws(() => readPrivateFile(path.join(dir, "loom-x.json")), /hard links/);
  assert.deepEqual(readJsonFile(path.join(dir, "loom-x.json")), {});
});

test("a rewrite replaces the whole file", { skip: !posix }, () => {
  const file = path.join(tmp(), "loom-x.json");
  assert.ok(writeJsonFile(file, { long: "a".repeat(100) }));
  assert.ok(writeJsonFile(file, { a: 1 }));
  assert.deepEqual(readJsonFile(file), { a: 1 });
});

test("a planted FIFO fails the write instead of hanging the hook", { skip: !posix }, () => {
  const fifo = path.join(tmp(), "loom-fifo");
  if (spawnSync("mkfifo", [fifo]).status !== 0) return;
  assert.equal(writeJsonFile(fifo, { a: 1 }), false);
});

test("a planted symlink is not read as state", { skip: !posix }, () => {
  const dir = tmp();
  const target = path.join(dir, "victim.json");
  fs.writeFileSync(target, JSON.stringify({ role: "user", content: "planted" }) + "\n");
  fs.symlinkSync(target, journalPath("s3", dir));
  assert.deepEqual(readJournal("s3", { dir }), []);
  const link = path.join(dir, "loom-x.json");
  fs.symlinkSync(target, link);
  assert.deepEqual(readJsonFile(link), {});
});

test("SessionStart sweeps this user's state files untouched for a day", { skip: !posix }, () => {
  const dir = tmp();
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  assert.ok(appendTurn("old", "user", "hello", { dir }));
  assert.ok(appendTurn("new", "user", "hello", { dir }));
  const oldStash = path.join(dir, "loom-recalled-old.json");
  const oldBatch = path.join(dir, "loom-shunt-pending.log");
  const other = path.join(dir, "loom-notes.txt");
  const link = path.join(dir, "loom-code-trace-link.json");
  fs.writeFileSync(oldStash, "[]");
  fs.writeFileSync(oldBatch, "");
  fs.writeFileSync(other, "not ours to sweep");
  fs.symlinkSync(other, link);
  const old = new Date(now - day - 1000);
  for (const f of [journalPath("old", dir), oldStash, oldBatch, other]) fs.utimesSync(f, old, old);
  fs.lutimesSync(link, old, old);
  assert.equal(sweepStaleStateFiles({ tmp: dir, now }), 3);
  assert.equal(fs.existsSync(journalPath("old", dir)), false);
  assert.equal(fs.existsSync(oldStash), false);
  assert.equal(fs.existsSync(oldBatch), false);
  assert.ok(fs.existsSync(journalPath("new", dir)), "a live session's journal stays");
  assert.ok(fs.existsSync(other) && fs.lstatSync(link).isSymbolicLink(), "other files and links stay");
});

test("SessionEnd keeps the turn journal and removes the stashes", { skip: !posix }, () => {
  const dir = tmp();
  const sid = "s-end";
  assert.ok(appendTurn(sid, "user", "hello", { dir }));
  const stash = path.join(dir, `loom-code-trace-${sid}.json`);
  const recalled = path.join(dir, `loom-recalled-${sid}.json`);
  fs.writeFileSync(stash, "[]");
  fs.writeFileSync(recalled, "[]");
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
  assert.ok(fs.existsSync(journalPath(sid, dir)), "the parallel SessionEnd hooks still read it");
  assert.equal(fs.existsSync(stash), false, "code-trace stash removed");
  assert.equal(fs.existsSync(recalled), false, "recalled stash removed");
});
