// The detached flusher keeps its cursor in a shared tmp dir: a symlink planted
// at the cursor path is refused, and the file it points to stays as it was.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hostEnv } from "./host-env.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-flush.mjs", import.meta.url));

async function server(status = 200) {
  const srv = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  return { srv, url: `http://127.0.0.1:${srv.address().port}` };
}

function flush(dir, url, job) {
  const jobPath = path.join(dir, "loom-flush-1700000000000-1.json");
  fs.writeFileSync(jobPath, JSON.stringify(job), { mode: 0o600 });
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, jobPath], {
      env: hostEnv({ PATH: process.env.PATH, HOME: dir, TMPDIR: dir, LOOM_BASE_URL: url, LOOM_API_KEY: "t.tok" }),
    });
    child.on("close", () => resolve(jobPath));
  });
}

const append = { org: "o", namespace: "n", session_id: "s", turns: [{ role: "user", content: "hi" }] };

const posix = process.platform !== "win32";

test("the flusher advances a cursor it owns and never follows a symlink at the cursor path", {
  skip: !posix,
}, async () => {
  const { srv, url } = await server();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-flushhook-"));
  try {
    const own = path.join(dir, "loom-conv-a.offset");
    const jobPath = await flush(dir, url, { appends: [append], cursorPath: own, advanceTo: 42 });
    assert.equal(fs.readFileSync(own, "utf8"), "42");
    assert.equal(fs.existsSync(jobPath), false);

    const victim = path.join(dir, "victim.txt");
    fs.writeFileSync(victim, "keep");
    const planted = path.join(dir, "loom-conv-b.offset");
    fs.symlinkSync(victim, planted);
    const plantedJob = await flush(dir, url, { appends: [append], cursorPath: planted, advanceTo: 42 });
    assert.equal(fs.readFileSync(victim, "utf8"), "keep");
    assert.equal(fs.existsSync(plantedJob), false);
    assert.ok(fs.lstatSync(planted).isSymbolicLink());
  } finally {
    srv.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a failed post rolls back a cursor it owns and never a symlink at the cursor path", { skip: !posix }, async () => {
  const { srv, url } = await server(500);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-flushhook-"));
  const job = (cursorPath) => ({
    posts: [{ op: "memory.set", body: { k: 1 } }],
    cursorPath,
    advanceTo: 42,
    rollbackTo: 7,
  });
  try {
    const own = path.join(dir, "loom-brief-a.offset");
    fs.writeFileSync(own, "42", { mode: 0o600 });
    await flush(dir, url, job(own));
    assert.equal(fs.readFileSync(own, "utf8"), "7");

    const victim = path.join(dir, "victim.txt");
    fs.writeFileSync(victim, "42");
    const planted = path.join(dir, "loom-brief-b.offset");
    fs.symlinkSync(victim, planted);
    const jobPath = await flush(dir, url, job(planted));
    assert.equal(fs.readFileSync(victim, "utf8"), "42");
    assert.ok(fs.lstatSync(planted).isSymbolicLink());
    assert.equal(fs.existsSync(jobPath), false);
  } finally {
    srv.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
