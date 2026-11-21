import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { fork, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import http from "node:http";
import path from "node:path";
import { once } from "node:events";
import { setImmediate as immediate } from "node:timers/promises";
import { removeSandbox } from "./hook-sandbox.mjs";
import { hostEnv } from "./host-env.mjs";
import { dispatchFlush } from "../lib/flush-job.mjs";

function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-hook-drain-"));
  const tmp = path.join(dir, "tmp");
  fs.mkdirSync(tmp);
  return { dir, tmp };
}

test("sandbox removal waits for its detached job's final write", async () => {
  const box = sandbox();
  const script = path.join(box.dir, "writer.cjs");
  fs.writeFileSync(
    script,
    `
const fs = require("node:fs");
const path = require("node:path");
const root = process.argv[2];
const job = path.join(root, "tmp", "loom-flush-1-" + process.pid + ".json");
fs.writeFileSync(job, "{}");
process.send("ready");
process.once("message", () => {
  fs.writeFileSync(path.join(root, "cursor"), "done");
  fs.unlinkSync(job);
  process.send("written", () => process.disconnect());
});
`,
  );
  const child = fork(script, [box.dir], {
    env: hostEnv({}),
    stdio: ["ignore", "ignore", "pipe", "ipc"],
    detached: true,
  });
  const closed = once(child, "close");
  let removing;
  let released = false;
  let stderr = "";
  child.stderr.on("data", (part) => {
    stderr += part;
  });
  try {
    const [ready] = await once(child, "message");
    assert.equal(ready, "ready");
    let removed = false;
    removing = removeSandbox(box).then(() => {
      removed = true;
    });
    await immediate();
    assert.equal(removed, false, "the owned writer still has a pending job");
    assert.equal(fs.existsSync(box.dir), true);
    const written = once(child, "message");
    released = true;
    child.send("release");
    assert.equal((await written)[0], "written");
    await removing;
    assert.deepEqual(await closed, [0, null], stderr);
    assert.equal(fs.existsSync(box.dir), false);
  } finally {
    if (!released && child.connected) child.send("release");
    await closed;
    if (removing) await removing;
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("an unfinished job refuses removal at the cleanup deadline", async () => {
  const box = sandbox();
  const job = path.join(box.tmp, "loom-flush-2-3.json");
  fs.writeFileSync(job, "{}");
  try {
    await assert.rejects(removeSandbox(box, { timeoutMs: 0 }), /unfinished detached jobs/);
    assert.equal(fs.existsSync(job), true);
  } finally {
    fs.rmSync(box.dir, { recursive: true, force: true });
  }
});

test("an empty job census permits removal with other sandbox files", async () => {
  const box = sandbox();
  fs.writeFileSync(path.join(box.tmp, "cursor"), "42");
  await removeSandbox(box);
  assert.equal(fs.existsSync(box.dir), false);
});

test("sandbox removal waits for the real flusher's completion marker", async () => {
  const box = sandbox();
  let arrived;
  const request = new Promise((resolve) => {
    arrived = resolve;
  });
  let respond;
  const server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      respond = () => {
        if (!res.writableEnded) {
          res.writeHead(200, { "content-type": "application/json" });
          res.end("{}");
        }
      };
      arrived();
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let child;
  let closed;
  let removing;
  const token = [
    "e30",
    Buffer.from(JSON.stringify({ org: "test", namespaces: ["scope"] })).toString("base64url"),
    "test",
  ].join(".");
  try {
    assert.equal(
      dispatchFlush(
        {
          appends: [{ org: "test", namespace: "scope", agent: "test", session_id: "test" }],
          cursorPath: path.join(box.tmp, "cursor"),
          advanceTo: 42,
        },
        {
          tmp: box.tmp,
          timeoutMs: 1000,
          spawnImpl: (command, args, options) => {
            child = spawn(command, args, {
              ...options,
              env: hostEnv({
                HOME: box.dir,
                TMPDIR: box.tmp,
                LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
                LOOM_API_KEY: token,
                LOOM_HOOK_TIMEOUT_MS: "1000",
                LOOM_AUTO_SIGNUP: "off",
              }),
            });
            closed = once(child, "close");
            return child;
          },
        },
      ),
      true,
    );
    await Promise.race([
      request,
      closed.then(() => {
        throw new Error("flusher exited before its request");
      }),
    ]);
    let removed = false;
    removing = removeSandbox(box).then(() => {
      removed = true;
    });
    await immediate();
    assert.equal(removed, false);
    assert.equal(fs.existsSync(box.dir), true);
    respond();
    assert.deepEqual(await closed, [0, null]);
    await removing;
    assert.equal(fs.existsSync(box.dir), false);
  } finally {
    respond?.();
    try {
      if (closed) await closed;
      if (removing) await removing;
    } finally {
      await new Promise((resolve) => server.close(resolve));
      fs.rmSync(box.dir, { recursive: true, force: true });
    }
  }
});
