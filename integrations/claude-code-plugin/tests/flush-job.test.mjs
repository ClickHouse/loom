import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { dispatchFlush, flushJobPath, runFlushJob } from "../lib/flush-job.mjs";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "loom-flushjob-"));

function memCursor(initial = {}) {
  const files = { ...initial };
  return { files, read: (p) => files[p] ?? "", write: (p, n) => (files[p] = String(n)) };
}

test("job files carry the name the client status check counts", () => {
  assert.match(path.basename(flushJobPath("/t", 1_700_000_000_000, 42)), /^loom-flush-\d{13}-\d+\.json$/);
});

test("dispatchFlush writes the job and spawns the flusher detached with the request bound", () => {
  const dir = tmp();
  const calls = [];
  const spawnImpl = (cmd, args, opts) => {
    calls.push({ cmd, args, opts });
    const child = new EventEmitter();
    child.unref = () => {};
    return child;
  };
  assert.equal(dispatchFlush({ posts: [{ op: "x", body: {} }] }, { timeoutMs: 1234, spawnImpl, tmp: dir }), true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].args[0], /hooks\/loom-flush\.mjs$/);
  assert.equal(calls[0].opts.detached, true);
  assert.equal(calls[0].opts.env.LOOM_HOOK_TIMEOUT_MS, "1234");
  const job = JSON.parse(fs.readFileSync(calls[0].args[1], "utf8"));
  assert.deepEqual(job, { posts: [{ op: "x", body: {} }] });
});

test("a spawn that throws leaves no job behind; a later spawn error does not crash the hook", () => {
  const dir = tmp();
  const throwing = () => {
    throw new Error("EAGAIN");
  };
  assert.equal(dispatchFlush({ posts: [] }, { spawnImpl: throwing, tmp: dir }), false);
  assert.deepEqual(fs.readdirSync(dir), []);
  const child = new EventEmitter();
  child.unref = () => {};
  assert.equal(dispatchFlush({ posts: [] }, { spawnImpl: () => child, tmp: dir }), true);
  assert.doesNotThrow(() => child.emit("error", new Error("spawn failed")));
});

test("appends advance the cursor only when every one landed", async () => {
  const job = { appends: [{ msg_id: "a" }, { msg_id: "b" }], cursorPath: "/c", advanceTo: 99 };
  const ok = memCursor({ "/c": "10" });
  assert.deepEqual(await runFlushJob(job, { post: async () => ({ stored: true }), cursor: ok }), {
    postsOk: false,
    appendsOk: true,
  });
  assert.equal(ok.files["/c"], "99");
  const partial = memCursor({ "/c": "10" });
  let n = 0;
  await runFlushJob(job, { post: async () => (n++ ? null : { stored: true }), cursor: partial });
  assert.equal(partial.files["/c"], "10", "one failed append keeps the cursor for a retry");
  const thrown = memCursor({ "/c": "10" });
  await runFlushJob(job, { post: async () => Promise.reject(new Error("boom")), cursor: thrown });
  assert.equal(thrown.files["/c"], "10");
});

test("the replies reach the recording note", async () => {
  const seen = [];
  await runFlushJob(
    { appends: [{ org: "o", namespace: "n" }] },
    {
      post: async () => ({ stored: false, reason: "logging_disabled" }),
      cursor: memCursor(),
      noteRecording: (a, r) => seen.push([a, r]),
    },
  );
  assert.deepEqual(seen, [[[{ org: "o", namespace: "n" }], [{ stored: false, reason: "logging_disabled" }]]]);
});

test("a failed optimistic post rolls its cursor back unless a later job moved it", async () => {
  const job = {
    posts: [{ op: "session.brief_save", body: { a: 1 } }],
    cursorPath: "/b",
    advanceTo: 50,
    rollbackTo: 20,
  };
  const failed = memCursor({ "/b": "50" });
  assert.equal((await runFlushJob(job, { post: async () => null, cursor: failed })).postsOk, false);
  assert.equal(failed.files["/b"], "20");
  const movedOn = memCursor({ "/b": "80" });
  await runFlushJob(job, { post: async () => null, cursor: movedOn });
  assert.equal(movedOn.files["/b"], "80");
  const landed = memCursor({ "/b": "50" });
  assert.equal((await runFlushJob(job, { post: async () => ({ ok: true }), cursor: landed })).postsOk, true);
  assert.equal(landed.files["/b"], "50");
});

test("malformed jobs deliver nothing", async () => {
  const calls = [];
  const post = async (op) => (calls.push(op), {});
  await runFlushJob({ posts: [null, { op: 1, body: {} }, { op: "x" }], appends: "no" }, { post, cursor: memCursor() });
  await runFlushJob(null, { post, cursor: memCursor() });
  assert.deepEqual(calls, []);
});
