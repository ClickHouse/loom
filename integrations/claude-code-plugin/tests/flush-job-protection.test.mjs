import "./temp-home.mjs";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";

import { dispatchFlush, runFlushJob } from "../lib/flush-job.mjs";
import { requestJson } from "../lib/loom-client.mjs";
import { createProtectionRoot, protectSession, sessionIsProtected } from "../lib/session-protection.mjs";
import {
  sessionProtectionContext,
  withAutomaticSessionProtection,
  withSessionProtection,
} from "../lib/session-response.mjs";

const owned = new Set();
function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "loom-flush-protection-"));
  owned.add(parent);
  const directory = process.platform === "win32" ? path.join(parent, "private") : parent;
  return { directory, root: createProtectionRoot({ base: directory }) };
}
afterEach(() => {
  for (const directory of owned) fs.rmSync(directory, { recursive: true, force: true });
  owned.clear();
});

function cursor() {
  return {
    value: "10",
    read() {
      return this.value;
    },
    write(_path, value) {
      this.value = String(value);
    },
  };
}

test("automatic contexts refuse missing protection roots", () => {
  let ran = false;
  withAutomaticSessionProtection({ base: path.resolve("missing"), id: "00000000-0000-0000-0000-000000000000" }, () => {
    ran = true;
  });
  assert.equal(ran, false);
});
{
  test("unknown and protected producers leave no queued content or child", () => {
    const { directory, root } = fixture();
    const before = fs.readdirSync(directory);
    let spawns = 0;
    const options = {
      tmp: directory,
      spawnImpl() {
        spawns++;
        throw new Error("must not spawn");
      },
    };
    assert.equal(dispatchFlush({ posts: [{ op: "memory.set_from_messages", body: {} }] }, options), false);
    protectSession(root);
    assert.equal(
      withSessionProtection(root, () => dispatchFlush({ appends: [{}] }, options)),
      false,
    );
    assert.equal(spawns, 0);
    assert.deepEqual(fs.readdirSync(directory), before);
  });

  test("a queued job captures its producer root rather than a supplied replacement", () => {
    const { directory, root } = fixture();
    const replacement = createProtectionRoot({ base: directory });
    let job;
    const options = {
      tmp: directory,
      spawnImpl(_command, args) {
        job = JSON.parse(fs.readFileSync(args[1], "utf8"));
        const child = new EventEmitter();
        child.unref = () => {};
        return child;
      },
    };
    assert.equal(
      withAutomaticSessionProtection(root, () =>
        dispatchFlush({ protectionRoots: [replacement], appends: [{}] }, options),
      ),
      true,
    );
    assert.deepEqual(job.protectionRoots, [root]);
    protectSession(root);
    assert.equal(sessionIsProtected(replacement), false);
  });

  test("late protection discards a detached job despite a different clean caller", async () => {
    const { root, directory } = fixture();
    const replacement = createProtectionRoot({ base: directory });
    const job = { protectionRoots: [root], appends: [{}], cursorPath: "/cursor", advanceTo: 99 };
    protectSession(root);
    let sends = 0;
    const position = cursor();
    const result = await withSessionProtection(replacement, () =>
      runFlushJob(job, {
        post: async () => {
          sends++;
          return { stored: true };
        },
        cursor: position,
      }),
    );
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
    assert.equal(sends, 0);
    assert.equal(position.value, "10");
  });

  test("a marked post prevents the append phase and success cursor", async () => {
    const { root } = fixture();
    const position = cursor();
    const sends = [];
    let notes = 0;
    const result = await runFlushJob(
      {
        protectionRoots: [root],
        posts: [{ op: "session.brief_save", body: {} }],
        appends: [{}],
        cursorPath: "/cursor",
        advanceTo: 99,
      },
      {
        async post(op, body) {
          sends.push(op);
          assert.equal(sessionProtectionContext().automatic, true);
          return (
            await requestJson(`http://server/v1/${op}`, {
              body,
              fetchImpl: async () => ({ status: 200, json: async () => ({ mirrored: true }) }),
            })
          ).body;
        },
        cursor: position,
        noteRecording() {
          notes++;
        },
      },
    );
    assert.deepEqual(sends, ["session.brief_save"]);
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
    assert.equal(position.value, "10");
    assert.equal(notes, 0);
    assert.equal(sessionIsProtected(root), true);
  });

  test("each dispatch rechecks protection even within the same parallel phase", async () => {
    const { root } = fixture();
    let sends = 0;
    const result = await runFlushJob(
      {
        protectionRoots: [root],
        posts: [
          { op: "session.brief_save", body: {} },
          { op: "memory.set_from_messages", body: {} },
        ],
      },
      {
        async post() {
          sends++;
          protectSession(root);
          return { stored: true };
        },
        cursor: cursor(),
      },
    );
    assert.equal(sends, 1);
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
  });

  test("a damaged queued root refuses delivery and a protected final reply never advances", async () => {
    const { root, directory } = fixture();
    const position = cursor();
    let sends = 0;
    const job = { protectionRoots: [root], appends: [{}], cursorPath: "/cursor", advanceTo: 99 };
    const io = {
      async post() {
        sends++;
        protectSession(root);
        return { stored: true };
      },
      cursor: position,
    };
    assert.equal((await runFlushJob(job, io)).appendsOk, false);
    assert.equal(sends, 1);
    assert.equal(position.value, "10");
    fs.unlinkSync(path.join(directory, root.id, "state"));
    await runFlushJob(job, io);
    await runFlushJob({ appends: [{}] }, io);
    assert.equal(sends, 1);
  });

  test("queued root identity cannot switch across an awaited post", async () => {
    const { root, directory } = fixture();
    const replacement = createProtectionRoot({ base: directory });
    const reference = { ...root };
    const position = cursor();
    let sends = 0;
    const result = await runFlushJob(
      {
        protectionRoots: [reference],
        posts: [{ op: "session.brief_save", body: {} }],
        appends: [{}],
        cursorPath: "/cursor",
        advanceTo: 99,
      },
      {
        async post() {
          sends++;
          protectSession(root);
          Object.assign(reference, replacement);
          await Promise.resolve();
          return { stored: true };
        },
        cursor: position,
      },
    );
    assert.equal(sends, 1);
    assert.equal(position.value, "10");
    assert.deepEqual(result, { postsOk: false, appendsOk: false });
  });

  test("suppressed optimistic jobs roll back rather than acknowledging unsent posts", async () => {
    const { root } = fixture();
    protectSession(root);
    for (const protectionRoots of [undefined, [root]]) {
      const position = cursor();
      position.value = "99";
      let sends = 0;
      const result = await runFlushJob(
        {
          protectionRoots,
          posts: [{ op: "session.brief_save", body: {} }],
          cursorPath: "/cursor",
          advanceTo: 99,
          rollbackTo: 10,
        },
        {
          post: async () => {
            sends++;
            return { stored: true };
          },
          cursor: position,
        },
      );
      assert.deepEqual(result, { postsOk: false, appendsOk: false });
      assert.equal(sends, 0);
      assert.equal(position.value, "10");
    }
  });
}
