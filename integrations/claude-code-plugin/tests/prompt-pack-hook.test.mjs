// loom-prompt-pack as the host runs it: on, in an indexed checkout, against a
// server that answers code.search, it injects the index's pointers — and it
// never fails silently on its own code (a missing import once made every
// prompt throw inside runHookMain, which swallows the error).
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { cachePathFor } from "../lib/repo-scope.mjs";

const HOOK = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "hooks", "loom-prompt-pack.mjs");

function checkout() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-pack-")));
  fs.writeFileSync(path.join(dir, "a.cpp"), "int f() { return 1; }\n");
  const g = (...a) =>
    execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", ...a], {
      cwd: dir,
      stdio: ["ignore", "pipe", "ignore"],
    });
  g("init", "-q");
  g("remote", "add", "origin", "git@github.com:x/y.git");
  g("add", ".");
  g("commit", "-qm", "c");
  return dir;
}

function run(stdin, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, "UserPromptSubmit"], { env, stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (err += c));
    child.on("close", (code) => resolve({ code, out, err }));
    child.stdin.end(JSON.stringify(stdin));
  });
}

test("on, in an indexed checkout: one code.search, the pointers injected, no hook error", async () => {
  const dir = checkout();
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-pack-tmp-")));
  fs.writeFileSync(
    cachePathFor(dir, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push({ url: req.url, body: JSON.parse(body || "{}") });
      res.setHeader("content-type", "application/json");
      res.end(
        JSON.stringify({
          abstain: false,
          retrieval_confidence: 0.6,
          results: [{ path: "src/Merge/Executor.h", start_line: 10, end_line: 40, symbol: "DB::Executor" }],
        }),
      );
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    const r = await run(
      { session_id: "s", cwd: dir, hook_event_name: "UserPromptSubmit", prompt: "where are merges scheduled" },
      {
        PATH: process.env.PATH,
        HOME: tmp,
        TMPDIR: tmp,
        LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
        LOOM_API_KEY: "t",
        LOOM_PROMPT_PACK: "on",
        LOOM_HOOK_DEBUG: "1",
      },
    );
    assert.equal(r.code, 0);
    assert.equal(r.err, "", "the hook reports no error of its own");
    assert.deepEqual(
      seen.map((s) => s.url),
      ["/v1/code.search"],
    );
    const ctx = JSON.parse(r.out).hookSpecificOutput.additionalContext;
    assert.match(ctx, /src\/Merge\/Executor\.h:10-40 DB::Executor/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});
