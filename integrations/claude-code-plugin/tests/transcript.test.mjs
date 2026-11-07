import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  completeLines,
  parseLines,
  readAllLines,
  readHeadTail,
  readLinesFrom,
  readRange,
  readTail,
} from "../lib/transcript.mjs";

const tmpFile = (text) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-transcript-"));
  const file = path.join(dir, "t.jsonl");
  fs.writeFileSync(file, text);
  return file;
};

test("readAllLines reads every object, and nothing past the cap", () => {
  const f = tmpFile('{"type":"assistant"}\nnot json\n\n{"type":"user"}\n');
  assert.deepEqual(readAllLines(f), [{ type: "assistant" }, { type: "user" }]);
  assert.deepEqual(readAllLines(f, 10), [], "over the cap");
  assert.deepEqual(readAllLines(path.join(path.dirname(f), "none.jsonl")), []);
  assert.deepEqual(readAllLines(""), []);
});

test("parseLines tolerates partial and foreign lines", () => {
  assert.equal(parseLines(`{"a":1}\n{broken\n\n{"b":2}\n`).length, 2);
  assert.deepEqual(parseLines(undefined), []);
});

test("completeLines leaves out a last line without its newline, and a partial head on request", () => {
  const buf = Buffer.from("aa\nbbb\ncc");
  assert.deepEqual(completeLines(buf, 100), [
    { text: "aa", start: 100, end: 103 },
    { text: "bbb", start: 103, end: 107 },
  ]);
  assert.deepEqual(
    completeLines(buf, 0, { partialHead: true }).map((l) => l.text),
    ["bbb"],
  );
  assert.deepEqual(completeLines(Buffer.from("no newline"), 0), []);
});

test("readTail and readHeadTail cut at line boundaries and read small files whole", () => {
  const file = tmpFile("line-one\nline-two\nline-three\n");
  assert.equal(readTail(file, 1000), "line-one\nline-two\nline-three\n");
  assert.equal(readTail(file, 15), "line-three\n", "the partial line the window began in is dropped");
  assert.equal(readHeadTail(file, 12, 12), "line-one\nline-three\n");
  assert.equal(readHeadTail(file, 1000, 1000), "line-one\nline-two\nline-three\n");
  assert.equal(readTail("/nonexistent/loom/transcript", 10), "");
  assert.equal(readRange("/nonexistent/loom/transcript", 0, 10).length, 0);
});

test("readLinesFrom reads a bounded window of complete lines from a byte cursor", () => {
  const file = tmpFile('{"n":1}\n{"n":2}\n{"n":3}\n{"n":');
  const all = readLinesFrom(file, 0, 1 << 20);
  assert.deepEqual(parseLines(all.lines.map((l) => l.text).join("\n")), [{ n: 1 }, { n: 2 }, { n: 3 }]);
  assert.equal(all.next, 24, "the half-written last line is left for the next read");
  const window = readLinesFrom(file, 0, 17);
  assert.equal(window.lines.length, 2);
  assert.equal(window.next, 16);
  assert.equal(readLinesFrom(file, 16, 1 << 20).lines.length, 1);
  assert.equal(readLinesFrom(file, 24, 1 << 20).next, 24, "no complete line yet: the cursor stays");
  assert.equal(readLinesFrom(file, 9999, 1 << 20).lines.length, 3, "a cursor past the end reads from 0");
});

test("readLinesFrom steps over one line longer than its window", () => {
  const file = tmpFile(`${"x".repeat(100)}\n{"n":2}\n`);
  const first = readLinesFrom(file, 0, 16);
  assert.deepEqual(first.lines, []);
  assert.equal(first.next, 101);
  assert.deepEqual(
    readLinesFrom(file, first.next, 16).lines.map((l) => l.text),
    ['{"n":2}'],
  );
});

// ---- the conversation hook's cursor over a real transcript ----

const HOOK = fileURLToPath(new URL("../hooks/loom-conversation.mjs", import.meta.url));

function fakeToken() {
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "none" })}.${b64({ org: "o", namespaces: ["coding"], sub: "t" })}.sig`;
}

const turn = (i) =>
  JSON.stringify(
    i % 2
      ? { type: "assistant", uuid: `a${i}`, message: { content: [{ type: "text", text: `reply ${i}` }] } }
      : { type: "user", uuid: `u${i}`, message: { content: `prompt ${i}` } },
  );

async function withLoom(fn) {
  const appended = [];
  appended.bodies = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      if (req.url === "/v1/conversation.append") {
        appended.push(JSON.parse(body).msg_id);
        appended.bodies.push(body);
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ stored: true }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, appended);
  } finally {
    server.close();
  }
}

function stop(env, payload) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, "Stop"], { env, stdio: ["pipe", "ignore", "ignore"] });
    child.on("exit", resolve);
    child.stdin.end(JSON.stringify(payload));
  });
}

// The detached flusher may still be writing its job file here when a test
// ends, so the temp dir is removed best-effort.
function removeTmp(dir) {
  try {
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  } catch {
    /* left for the OS to clear */
  }
}

async function until(pred, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (pred()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

test("a Stop with more than fifty new turns sends fifty and leaves the rest for the next Stop", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-conv-cursor-"));
  const transcript = path.join(dir, "t.jsonl");
  fs.writeFileSync(transcript, Array.from({ length: 60 }, (_, i) => turn(i)).join("\n") + "\n");
  await withLoom(async (url, appended) => {
    const env = { PATH: process.env.PATH, HOME: dir, TMPDIR: dir, LOOM_BASE_URL: url, LOOM_API_KEY: fakeToken() };
    env.LOOM_CONV_SETTLE_MS = "0";
    const payload = { session_id: "conv-1", transcript_path: transcript, cwd: dir };
    await stop(env, payload);
    assert.ok(await until(() => appended.length === 50), `first Stop sent ${appended.length}`);
    const cursor = path.join(dir, "loom-conv-conv-1.offset");
    assert.ok(await until(() => fs.existsSync(cursor)), "the flusher moved the cursor");
    await stop(env, payload);
    assert.ok(await until(() => appended.length === 60), `after two Stops: ${appended.length}`);
    assert.deepEqual(new Set(appended).size, 60, "no turn sent twice, none skipped");
  });
  removeTmp(dir);
});

test("a turn still being written when Stop fires is sent by the next Stop", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-conv-partial-"));
  const transcript = path.join(dir, "t.jsonl");
  const last = turn(3);
  fs.writeFileSync(transcript, [turn(0), turn(1), turn(2)].join("\n") + "\n" + last.slice(0, 20));
  await withLoom(async (url, appended) => {
    const env = { PATH: process.env.PATH, HOME: dir, TMPDIR: dir, LOOM_BASE_URL: url, LOOM_API_KEY: fakeToken() };
    env.LOOM_CONV_SETTLE_MS = "0";
    const payload = { session_id: "conv-2", transcript_path: transcript, cwd: dir };
    await stop(env, payload);
    assert.ok(await until(() => appended.length === 3));
    assert.ok(await until(() => fs.existsSync(path.join(dir, "loom-conv-conv-2.offset"))));
    fs.appendFileSync(transcript, last.slice(20) + "\n");
    await stop(env, payload);
    assert.ok(await until(() => appended.includes("a3")), `sent: ${appended.join(",")}`);
  });
  removeTmp(dir);
});

test("a Stop redacts a secret in each turn it sends, from the transcript and from the event", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-conv-redact-"));
  const secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8";
  const transcript = path.join(dir, "t.jsonl");
  const said = { type: "user", uuid: "u0", message: { content: `my token is ${secret}` } };
  fs.writeFileSync(transcript, JSON.stringify(said) + "\n");
  await withLoom(async (url, appended) => {
    const env = { PATH: process.env.PATH, HOME: dir, TMPDIR: dir, LOOM_BASE_URL: url, LOOM_API_KEY: fakeToken() };
    env.LOOM_CONV_SETTLE_MS = "0";
    await stop(env, { session_id: "conv-r1", transcript_path: transcript, cwd: dir });
    await stop(env, { session_id: "conv-r2", last_assistant_message: `use ${secret} here`, cwd: dir });
    assert.ok(await until(() => appended.length === 2), `sent ${appended.length}`);
    for (const body of appended.bodies) {
      assert.ok(!body.includes(secret.slice(4)), body);
      assert.ok(body.includes("[redacted]"), body);
    }
  });
  removeTmp(dir);
});
