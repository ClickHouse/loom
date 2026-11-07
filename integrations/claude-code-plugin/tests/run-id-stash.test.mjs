// run.begin can answer another run id than the derived one; the run hook
// keeps it in the session's run state and every later hook uses it.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { completeBody, readRunState, sessionRunId, writeRunBegun } from "../lib/run-hook.mjs";
import { STALE_STATE_MS, sessionStatePath, sweepStaleStateFiles } from "../lib/tmp-state.mjs";
import { recallBody } from "../lib/prompt-recall.mjs";
import { sessionMemoryBody } from "../lib/session-memory.mjs";
import { hostEnv, withTmpdir } from "./host-env.mjs";

const HOOK = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "hooks", "loom-run.mjs");
const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: [] };

function freshTmp() {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-runid-")));
}

function runHook(event, payload, { tmp, url }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, event], {
      env: hostEnv({ PATH: process.env.PATH, HOME: tmp, TMPDIR: tmp, LOOM_BASE_URL: url, LOOM_API_KEY: "t" }),
      stdio: ["pipe", "ignore", "ignore"],
    });
    child.on("close", resolve);
    child.stdin.end(JSON.stringify(payload));
  });
}

// A fake server: run.begin answers `beginRunId` after `delayMs`; every request is recorded.
async function withServer(
  { beginRunId, delayMs = 0, beginStatus = 200, firstBeginDelayMs = 0, completeDelayMs = 0 },
  fn,
) {
  let begins = 0;
  const seen = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => {
      raw += c;
    });
    req.on("end", () => {
      const entry = { url: req.url, body: raw ? JSON.parse(raw) : {}, at: Date.now() };
      seen.push(entry);
      res.on("close", () => {
        entry.closedAt = Date.now();
      });
      const answer = req.url.endsWith("/run.begin") ? { run_id: beginRunId } : {};
      const first = req.url.endsWith("/run.begin") && begins++ === 0;
      setTimeout(
        () => {
          if (req.url.endsWith("/run.begin")) res.statusCode = beginStatus;
          res.setHeader("content-type", "application/json");
          res.end(JSON.stringify(answer));
        },
        delayMs + (first ? firstBeginDelayMs : 0) + (req.url.endsWith("/run.complete") ? completeDelayMs : 0),
      );
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
  // SessionEnd closed the run, but the other SessionEnd hooks still read its id.
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

test("a run.begin that times out is asked again, and the run it opens is completed", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s2", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  // Not begun: nothing was answered; only the attempt is marked.
  withTmpdir(tmp, () => {
    assert.equal(readRunState("s2").begun, undefined);
    assert.equal(readRunState("s2").attempted, true);
  });
  await withServer({ beginRunId: "0a1b2c3d" }, async (url, seen) => {
    const payload = { session_id: "s2", cwd: tmp, prompt: "next" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    const begin = seen.find((r) => r.url.endsWith("/run.begin"));
    const complete = seen.find((r) => r.url.endsWith("/run.complete"));
    assert.equal(begin.body.run_id, "cc-s2", "the retry asks for the same derived id");
    assert.equal(complete.body.run_id, "0a1b2c3d", "the run the retry opened is the one completed");
    assert.equal(complete.body.session_id, "s2");
  });
});

test("a run.begin refused with a 4xx is not asked again", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "x", beginStatus: 403 }, async (url, seen) => {
    const payload = { session_id: "s3", cwd: tmp, prompt: "p" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("UserPromptSubmit", { ...payload, prompt: "q" }, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    assert.equal(seen.filter((r) => r.url.endsWith("/run.begin")).length, 1, "asked once");
    assert.equal(seen.filter((r) => r.url.endsWith("/run.complete")).length, 0, "nothing to close");
  });
});

test("a run.begin that never answered is asked again at SessionEnd, and the run it answers is closed", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s4", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  await withServer({ beginRunId: "r0wn0f54" }, async (url, seen) => {
    await runHook("SessionEnd", { session_id: "s4", cwd: tmp, reason: "exit" }, { tmp, url });
    const begin = seen.find((r) => r.url.endsWith("/run.begin"));
    assert.equal(begin.body.run_id, "cc-s4", "asked again with the same derived id");
    assert.equal(begin.body.goal, "p", "and the same goal");
    assert.equal(seen.find((r) => r.url.endsWith("/run.complete")).body.run_id, "r0wn0f54");
  });
});

test("a begin whose answer was lost is recovered, not duplicated, on the next prompt", async () => {
  // One server: the first begin commits but its answer arrives after the hook's
  // timeout; the retry gets the same id (the server's begin is idempotent).
  const tmp = freshTmp();
  await withServer({ beginRunId: "r5ame1d0", firstBeginDelayMs: 2500 }, async (url, seen) => {
    const payload = { session_id: "s7", cwd: tmp, prompt: "p" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("UserPromptSubmit", { ...payload, prompt: "q" }, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    const begins = seen.filter((r) => r.url.endsWith("/run.begin"));
    assert.equal(begins.length, 2);
    assert.deepEqual(
      begins.map((r) => r.body.run_id),
      ["cc-s7", "cc-s7"],
      "the retry carries the same derived id",
    );
    assert.equal(seen.find((r) => r.url.endsWith("/run.complete")).body.run_id, "r5ame1d0");
  });
});

test("every retry re-sends the first begin's goal and request", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s9", cwd: tmp, prompt: "first ask" }, { tmp, url });
  });
  await withServer({ beginRunId: "x", beginStatus: 403 }, async (url, seen) => {
    const payload = { session_id: "s9", cwd: tmp, prompt: "a follow-up" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    const begins = seen.filter((r) => r.url.endsWith("/run.begin"));
    assert.equal(begins.length, 2, "the refused prompt and SessionEnd");
    for (const r of begins) {
      assert.equal(r.body.goal, "first ask");
      assert.equal(r.body.user_request, "first ask");
    }
  });
});

test("the retry state holds the redacted body, never the raw prompt", async () => {
  const tmp = freshTmp();
  const secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789";
  // One credential well inside the body, one straddling the request's cut.
  const prompt = `use ${secret} here ` + "x".repeat(8000 - 30) + ` ${secret} tail`;
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s10", cwd: tmp, prompt }, { tmp, url });
  });
  const raw = fs.readFileSync(
    withTmpdir(tmp, () => sessionStatePath("run", "s10")),
    "utf8",
  );
  assert.ok(withTmpdir(tmp, () => readRunState("s10")).attempted);
  for (const frag of ["ghp_", "abcdefghijklmnopqrstuvwxyz"]) assert.ok(!raw.includes(frag), frag);
  assert.equal(raw.includes(" tail"), false, "nothing past the request's cut");
});

test("a re-sent begin and the close fit Codex's one-second SessionEnd budget", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s11", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  // The re-sent begin answers inside its 400 ms cap with room for a loaded
  // machine's scheduling delay; the close would take past the deadline.
  await withServer({ beginRunId: "r5ess1on", delayMs: 200, completeDelayMs: 1100 }, async (url, seen) => {
    await runHook("SessionEnd", { session_id: "s11", cwd: tmp, reason: "exit" }, { tmp, url });
    const begin = seen.find((r) => r.url.endsWith("/run.begin"));
    const close = seen.find((r) => r.url.endsWith("/run.complete"));
    assert.ok(close, "the close was sent");
    // From the begin's arrival to the hook dropping the close: the hook's own time,
    // without node's start-up. The server would answer the close at 1500 ms.
    const spent = close.closedAt - begin.at;
    assert.ok(spent < 1000, `begin and close took ${spent} ms`);
  });
});

test("a 429 is a busy moment: the next prompt asks again", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "x", beginStatus: 429 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s5", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  withTmpdir(tmp, () => assert.equal(readRunState("s5").refused, undefined));
  await withServer({ beginRunId: "5a5a5a5a" }, async (url, seen) => {
    const payload = { session_id: "s5", cwd: tmp, prompt: "q" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    assert.equal(seen.find((r) => r.url.endsWith("/run.complete")).body.run_id, "5a5a5a5a");
  });
});

test("a refusal after an unanswered begin is asked once more at SessionEnd", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "late", delayMs: 2500 }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s6", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  await withServer({ beginRunId: "x", beginStatus: 403 }, async (url, seen) => {
    const payload = { session_id: "s6", cwd: tmp, prompt: "q" };
    await runHook("UserPromptSubmit", payload, { tmp, url });
    await runHook("SessionEnd", { ...payload, reason: "exit" }, { tmp, url });
    // The earlier attempt is kept through the refusal, so SessionEnd asks again; a
    // token refused run.begin is refused run.complete too, so nothing is closed.
    assert.equal(seen.filter((r) => r.url.endsWith("/run.begin")).length, 2);
    assert.equal(seen.filter((r) => r.url.endsWith("/run.complete")).length, 0);
  });
});

test("a 2xx without a usable run id leaves the begin attempted", async () => {
  const tmp = freshTmp();
  await withServer({ beginRunId: "" }, async (url) => {
    await runHook("UserPromptSubmit", { session_id: "s8", cwd: tmp, prompt: "p" }, { tmp, url });
  });
  withTmpdir(tmp, () => {
    assert.equal(readRunState("s8").attempted, true);
    assert.equal(readRunState("s8").refused, undefined);
  });
});

test("a session never reads another session's stored id", () => {
  const tmp = freshTmp();
  assert.ok(writeRunBegun("s1", "0123abcd", { tmp }));
  assert.equal(sessionRunId("s1", { tmp }), "0123abcd");
  assert.equal(sessionRunId("s2", { tmp }), "cc-s2");
  assert.equal(sessionRunId("", { tmp }), "", "no session, no run");
});

test("a malformed answer is not stored", () => {
  const tmp = freshTmp();
  writeRunBegun("s1", "../../etc", { tmp });
  assert.equal(sessionRunId("s1", { tmp }), "cc-s1");
  writeRunBegun("s1", 42, { tmp });
  assert.equal(sessionRunId("s1", { tmp }), "cc-s1");
  assert.equal(writeRunBegun("", "0123abcd", { tmp }), false);
});

test("a stored id read within the sweep age outlives the sweep", () => {
  const tmp = freshTmp();
  const old = new Date(Date.now() - STALE_STATE_MS - 60_000);
  writeRunBegun("s1", "0123abcd", { tmp });
  writeRunBegun("s2", "4567ef", { tmp });
  for (const sid of ["s1", "s2"]) fs.utimesSync(sessionStatePath("run", sid, { tmp }), old, old);
  assert.equal(sessionRunId("s1", { tmp }), "0123abcd"); // s1 is still running
  sweepStaleStateFiles({ tmp });
  assert.equal(sessionRunId("s1", { tmp }), "0123abcd");
  assert.equal(sessionRunId("s2", { tmp }), "cc-s2", "an idle session's state is swept");
});
