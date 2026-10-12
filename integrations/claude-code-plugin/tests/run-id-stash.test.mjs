// run.begin can answer another run id than the derived one; the run hook
// stores it per session and every later hook uses it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { completeBody, sessionRunId, storeRunId } from "../lib/run-hook.mjs";
import { recallBody } from "../lib/prompt-recall.mjs";
import { sessionMemoryBody } from "../lib/session-memory.mjs";

const HOOK = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "hooks", "loom-run.mjs");
const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: [] };

function freshTmp() {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-runid-")));
}

function runHook(event, payload, { tmp, url }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, event], {
      env: { PATH: process.env.PATH, HOME: tmp, TMPDIR: tmp, LOOM_BASE_URL: url, LOOM_API_KEY: "t" },
      stdio: ["pipe", "ignore", "ignore"],
    });
    child.on("close", resolve);
    child.stdin.end(JSON.stringify(payload));
  });
}

// A fake server: run.begin answers `beginRunId` after `delayMs`; every request is recorded.
async function withServer({ beginRunId, delayMs = 0 }, fn) {
  const seen = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => {
      raw += c;
    });
    req.on("end", () => {
      seen.push({ url: req.url, body: raw ? JSON.parse(raw) : {} });
      const answer = req.url.endsWith("/run.begin") ? { run_id: beginRunId } : {};
      setTimeout(() => {
        res.setHeader("content-type", "application/json");
        res.end(JSON.stringify(answer));
      }, delayMs);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen);
  } finally {
    server.closeAllConnections();
    server.close();
  }
}

// The in-process helpers read os.tmpdir(), which follows TMPDIR.
function withTmpdir(tmp, fn) {
  const prev = process.env.TMPDIR;
  process.env.TMPDIR = tmp;
  try {
    return fn();
  } finally {
    if (prev === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = prev;
  }
}

test("the id run.begin answers is used by run.complete, prompt recall and session memory", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "0123abcd" }, async (url, seen) => {
    const payload = { session_id: "s1", cwd: tmp, prompt: "fix the bug" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    const begin = seen.find((r) => r.url.endsWith("/run.begin"));
    const complete = seen.find((r) => r.url.endsWith("/run.complete"));
    assert.equal(begin.body.run_id, "cc-s1", "begin still asks for the derived id");
    assert.equal(complete.body.run_id, "0123abcd");
  });
  withTmpdir(tmp, () => {
    assert.equal(sessionRunId("s1"), "0123abcd");
    assert.equal(recallBody(scope, { session_id: "s1" }, "q").run_id, "0123abcd");
    const turns = [
      { role: "user", content: "remember that the build uses make" },
      { role: "assistant", content: "noted, the build uses make" },
    ];
    assert.equal(sessionMemoryBody(scope, { session_id: "s1" }, turns).run_id, "0123abcd");
  });
});

test("a run.begin that times out keeps the derived id", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s2", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  withTmpdir(tmp, () => {
    assert.equal(sessionRunId("s2"), "cc-s2");
    assert.equal(completeBody(scope, { session_id: "s2", reason: "exit" }, "").run_id, "cc-s2");
  });
});

test("a session never reads another session's stored id", () => {
  const tmp = freshTmp();
  assert.ok(storeRunId("s1", "0123abcd", tmp));
  assert.equal(sessionRunId("s1", { tmp }), "0123abcd");
  assert.equal(sessionRunId("s2", { tmp }), "cc-s2");
  assert.equal(sessionRunId("", { tmp }), "", "no session, no run");
});

test("a malformed answer is not stored", () => {
  const tmp = freshTmp();
  assert.equal(storeRunId("s1", "../../etc", tmp), false);
  assert.equal(storeRunId("s1", 42, tmp), false);
  assert.equal(storeRunId("", "0123abcd", tmp), false);
  assert.equal(sessionRunId("s1", { tmp }), "cc-s1");
});
