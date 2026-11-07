import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  readCursor,
  readJsonFile,
  readSessionState,
  sessionStatePath,
  tmpStatePath,
  touchPrivateFile,
  writeCursor,
  writeJsonFile,
  writeSessionState,
} from "../lib/tmp-state.mjs";

const sha12 = (s) => crypto.createHash("sha1").update(s).digest("hex").slice(0, 12);

test("state files keep the names hooks have always used (state survives an upgrade)", () => {
  assert.equal(sessionStatePath("run", "abc", { tmp: "/t" }), path.join("/t", `loom-run-${sha12("abc")}.json`));
  assert.equal(sessionStatePath("wm", "", { tmp: "/t" }), path.join("/t", `loom-wm-${sha12("nosession")}.json`));
  assert.equal(
    sessionStatePath("prompt-recall-first", "abc", { tmp: "/t", ext: "" }),
    path.join("/t", `loom-prompt-recall-first-${sha12("abc")}`),
  );
  assert.equal(
    tmpStatePath("taskq", "https://x|o|n", { tmp: "/t" }),
    path.join("/t", `loom-taskq-${sha12("https://x|o|n")}.json`),
  );
  assert.ok(!sessionStatePath("x", "../../etc/passwd", { tmp: "/t" }).includes(".."), "the id is hashed, never a path");
});

test("JSON and cursor files read their fallback when missing or malformed", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-tmpstate-"));
  const f = path.join(dir, "s.json");
  assert.deepEqual(readJsonFile(f), {});
  assert.equal(readJsonFile(f, null), null);
  fs.writeFileSync(f, "{nope");
  assert.deepEqual(readJsonFile(f, []), []);
  assert.equal(writeJsonFile(f, { a: 1 }), true);
  assert.deepEqual(readJsonFile(f), { a: 1 });
  assert.equal(writeJsonFile(path.join(dir, "missing", "x.json"), {}), false);
  const c = path.join(dir, "c.offset");
  assert.equal(readCursor(c), 0);
  writeCursor(c, 42);
  assert.equal(readCursor(c), 42);
  fs.writeFileSync(c, "garbage");
  assert.equal(readCursor(c), 0);
  fs.writeFileSync(c, "-5");
  assert.equal(readCursor(c), 0);
});

test("one session-state file per (hook, session); missing, garbage and non-objects read as {}", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-tmpstate-"));
  assert.match(path.basename(sessionStatePath("testgate", "s-1", { tmp })), /^loom-testgate-[0-9a-f]{12}\.json$/);
  assert.notEqual(sessionStatePath("testgate", "s-1", { tmp }), sessionStatePath("blast", "s-1", { tmp }));
  assert.deepEqual(readSessionState("blast", "s", { tmp }), {});
  assert.equal(writeSessionState("blast", "s", { sig: { "a.cpp": "x" } }, { tmp }), true);
  assert.deepEqual(readSessionState("blast", "s", { tmp }), { sig: { "a.cpp": "x" } });
  for (const junk of ["{not json", "[1,2]", "null", "7"]) {
    fs.writeFileSync(sessionStatePath("blast", "s", { tmp }), junk);
    assert.deepEqual(readSessionState("blast", "s", { tmp }), {}, junk);
  }
  assert.equal(writeSessionState("blast", "s", { ok: 1 }, { tmp: path.join(tmp, "missing-dir") }), false);
});

// Windows sets a file's times only through a handle that may write.
test("touchPrivateFile moves a state file's mtime to now, on every OS", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-tmpstate-"));
  const f = path.join(dir, "s.json");
  assert.equal(writeJsonFile(f, { a: 1 }), true);
  const old = new Date(Date.now() - 3_600_000);
  fs.utimesSync(f, old, old);
  assert.equal(touchPrivateFile(f), true);
  assert.ok(fs.statSync(f).mtimeMs > old.getTime() + 3_000_000);
  assert.equal(touchPrivateFile(path.join(dir, "missing.json")), false);
});
