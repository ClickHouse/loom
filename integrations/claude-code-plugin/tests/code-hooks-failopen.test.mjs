// The code-plane hooks run as the host runs them, against the failures a
// session meets: malformed or empty stdin, no token, a refused connection, a
// server that answers 401, a server that never answers, a re-entered Stop.
// Every case exits 0 within its bound and says nothing, or only what local
// policy decides without the server.
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
import { hostEnv } from "./host-env.mjs";

const HOOKS = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "hooks");
const BIG = Array.from({ length: 400 }, (_, i) => `int f${i}() { return ${i}; }`).join("\n") + "\n";
const REFUSED = "http://127.0.0.1:9";

function checkout() {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-failopen-")));
  fs.mkdirSync(path.join(dir, "src"));
  fs.writeFileSync(path.join(dir, "src/a.cpp"), BIG);
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

function freshTmp() {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-failopen-tmp-")));
}

function seedIndexed(root, tmp) {
  fs.writeFileSync(
    cachePathFor(root, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
}

/** Run one hook; `stdin` is a string (sent as is) or an object (JSON). */
function run(hook, stdin, { tmp, url = REFUSED, token = "t", env = {} }) {
  return new Promise((resolve) => {
    const started = Date.now();
    const child = spawn(process.execPath, [path.join(HOOKS, hook), "X"], {
      env: hostEnv({
        PATH: process.env.PATH,
        HOME: tmp,
        TMPDIR: tmp,
        LOOM_BASE_URL: url,
        LOOM_API_KEY: token,
        LOOM_SHUNT_REPORT: "off",
        ...env,
      }),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (c) => (out += c));
    child.stderr.on("data", (c) => (err += c));
    child.on("close", (code) => resolve({ code, out, err, ms: Date.now() - started, closedAt: Date.now() }));
    child.stdin.end(typeof stdin === "string" ? stdin : JSON.stringify(stdin));
  });
}

/** A server that answers every request with `status`, or never when null. */
async function withServer(status, fn) {
  const seen = [];
  // When each op last arrived: what a hook spends after its request is sent.
  const arrived = new Map();
  const server = http.createServer((req, res) => {
    seen.push(req.url);
    arrived.set(req.url, Date.now());
    if (status == null) return; // hang
    res.statusCode = status;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ error: "nope" }));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen, arrived);
  } finally {
    server.closeAllConnections();
    server.close();
  }
}

/** A server that records each request's url and JSON body and answers 500. */
async function withCapturingServer(fn) {
  const bodies = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      bodies.push({ url: req.url, body: raw ? JSON.parse(raw) : null });
      res.statusCode = 500;
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    await fn(`http://127.0.0.1:${server.address().port}`);
  } finally {
    server.closeAllConnections();
    server.close();
  }
  return bodies;
}

const HOOK_NAMES = [
  "loom-code-shunt.mjs",
  "loom-file-context.mjs",
  "loom-blast-radius.mjs",
  "loom-test-gate.mjs",
  "loom-validate-gate.mjs",
];

test("malformed, empty and non-object stdin: every code hook exits 0 and prints nothing", async () => {
  const tmp = freshTmp();
  for (const hook of HOOK_NAMES) {
    for (const stdin of ["{not json", "", "[]", "null", '"text"', "42"]) {
      const r = await run(hook, stdin, { tmp });
      assert.equal(r.code, 0, `${hook} on ${JSON.stringify(stdin)}`);
      assert.equal(r.out, "", `${hook} on ${JSON.stringify(stdin)}`);
      assert.equal(r.err, "", `${hook} writes no stack trace on ${JSON.stringify(stdin)}`);
    }
  }
});

test("no token: nothing is asked of the server and nothing is printed", async () => {
  const dir = checkout();
  await withServer(200, async (url, seen) => {
    const tmp = freshTmp();
    const read = {
      tool_name: "Read",
      tool_input: { file_path: path.join(dir, "src/a.cpp") },
      cwd: dir,
      session_id: "s",
    };
    const edit = {
      tool_name: "Edit",
      tool_input: { file_path: path.join(dir, "src/a.cpp") },
      cwd: dir,
      session_id: "s",
    };
    for (const [hook, payload] of [
      ["loom-code-shunt.mjs", read],
      ["loom-file-context.mjs", edit],
      ["loom-blast-radius.mjs", edit],
      ["loom-test-gate.mjs", { cwd: dir, session_id: "s" }],
      ["loom-validate-gate.mjs", { cwd: dir, session_id: "s" }],
    ]) {
      const r = await run(hook, payload, { tmp, url, token: "" });
      assert.equal(r.code, 0, hook);
      assert.equal(r.out, "", hook);
    }
    assert.deepEqual(seen, []);
  });
});

test("shunt: a refused connection with no cached entry passes and is retried soon, not cached as not-indexed", async () => {
  const dir = checkout();
  const tmp = freshTmp();
  const r = await run(
    "loom-code-shunt.mjs",
    { tool_name: "Read", tool_input: { file_path: path.join(dir, "src/a.cpp") }, cwd: dir, session_id: "s" },
    { tmp },
  );
  assert.equal(r.code, 0);
  assert.equal(r.out, "");
  const entry = JSON.parse(fs.readFileSync(cachePathFor(dir, tmp), "utf8"));
  assert.equal(entry.indexed, false);
  assert.equal(entry.unresolved, true);
});

test("shunt: a 401 from the repository lookup passes the call and records it unresolved", async () => {
  const dir = checkout();
  await withServer(401, async (url, seen) => {
    const tmp = freshTmp();
    const r = await run(
      "loom-code-shunt.mjs",
      { tool_name: "Read", tool_input: { file_path: path.join(dir, "src/a.cpp") }, cwd: dir, session_id: "s" },
      { tmp, url },
    );
    assert.equal(r.code, 0);
    assert.equal(r.out, "");
    assert.deepEqual(seen, ["/v1/code.repos"]);
    assert.equal(JSON.parse(fs.readFileSync(cachePathFor(dir, tmp), "utf8")).unresolved, true);
  });
});

test("shunt: a server that never answers holds the call no longer than the lookup bound", async () => {
  const dir = checkout();
  await withServer(null, async (url) => {
    const tmp = freshTmp();
    const r = await run(
      "loom-code-shunt.mjs",
      { tool_name: "Read", tool_input: { file_path: path.join(dir, "src/a.cpp") }, cwd: dir, session_id: "s" },
      { tmp, url, env: { LOOM_SHUNT_RESOLVE_TIMEOUT_MS: "1000" } },
    );
    assert.equal(r.code, 0);
    assert.equal(r.out, "");
    assert.ok(r.ms < 4000, `took ${r.ms} ms`);
  });
});

test("shunt: an indexed entry decides locally; a symbol grep whose answer hangs keeps the plain redirect", async () => {
  const dir = checkout();
  await withServer(null, async (url) => {
    const tmp = freshTmp();
    seedIndexed(dir, tmp);
    const r = await run(
      "loom-code-shunt.mjs",
      {
        tool_name: "Grep",
        tool_input: { pattern: "renameTempPartAndAdd", path: path.join(dir, "src/a.cpp") },
        cwd: dir,
        session_id: "s",
      },
      { tmp, url },
    );
    assert.equal(r.code, 0);
    const reason = JSON.parse(r.out).hookSpecificOutput.permissionDecisionReason;
    assert.match(reason, /names a symbol — call loom_code_symbol/);
    assert.ok(r.ms < 5000, `took ${r.ms} ms`);
  });
});

test("shunt: outside a git checkout, or a tool it does not judge, nothing happens", async () => {
  const tmp = freshTmp();
  const r = await run(
    "loom-code-shunt.mjs",
    { tool_name: "Read", tool_input: { file_path: path.join(tmp, "x") }, cwd: tmp, session_id: "s" },
    { tmp },
  );
  assert.equal(r.out, "");
  const dir = checkout();
  seedIndexed(dir, tmp);
  const w = await run("loom-code-shunt.mjs", { tool_name: "Write", tool_input: {}, cwd: dir }, { tmp });
  assert.equal(w.out, "");
});

/** A dirty indexed checkout and a transcript whose Edit named the file. */
function dirtySession() {
  const dir = checkout();
  fs.appendFileSync(path.join(dir, "src/a.cpp"), "int g() { return 1; }\n");
  const tmp = freshTmp();
  seedIndexed(dir, tmp);
  const transcript = path.join(tmp, "t.jsonl");
  fs.writeFileSync(
    transcript,
    JSON.stringify({
      type: "assistant",
      message: {
        content: [
          { type: "tool_use", name: "Edit", input: { file_path: path.join(dir, "src/a.cpp") } },
          { type: "tool_use", name: "mcp__loom__loom_memory_set", input: { memory_key: "k/a" } },
        ],
      },
    }) + "\n",
  );
  return { dir, tmp, transcript };
}

test("Stop gates: 401 and a hanging server both let the stop through", async () => {
  for (const status of [401, null]) {
    await withServer(status, async (url, seen, arrived) => {
      for (const hook of ["loom-test-gate.mjs", "loom-validate-gate.mjs"]) {
        const op = hook === "loom-test-gate.mjs" ? "/v1/code.test_gate" : "/v1/namespace.validate";
        // A run whose own git calls timed out (1.5 s each, on a loaded runner)
        // ends quietly before it asks, and tests nothing here: run again.
        let r;
        for (let attempt = 0; attempt < 3; attempt++) {
          const { dir, tmp, transcript } = dirtySession();
          arrived.delete(op);
          r = await run(
            hook,
            { cwd: dir, session_id: "s", transcript_path: transcript },
            { tmp, url, env: { LOOM_HOOK_TIMEOUT_MS: "500", LOOM_HOOK_EXIT_MS: "500" } },
          );
          assert.equal(r.code, 0, `${hook} ${status}`);
          assert.equal(r.out, "", `${hook} ${status}`);
          if (arrived.has(op)) break;
        }
        // From the gate's request to the hook's exit: the 500 ms request
        // bound and the 500 ms exit grace, not node's start-up or git, which
        // a loaded runner stretches.
        assert.ok(arrived.has(op), `${hook} ${status}: never sent ${op} in three runs`);
        const after = r.closedAt - arrived.get(op);
        assert.ok(after < 1500, `${hook} ${status}: ${after} ms after its request (${r.ms} ms in all)`);
        // And the whole run, start-up and git included: twice the slowest
        // seen on a loaded runner (3.4 s, macOS Intel).
        assert.ok(r.ms < 7000, `${hook} ${status} took ${r.ms} ms`);
      }
      assert.ok(seen.includes("/v1/code.test_gate") && seen.includes("/v1/namespace.validate"), seen.join(","));
    });
  }
});

test("Stop gates: a Stop the gate itself caused never blocks again", async () => {
  await withServer(200, async (url, seen) => {
    for (const hook of ["loom-test-gate.mjs", "loom-validate-gate.mjs"]) {
      const { dir, tmp, transcript } = dirtySession();
      const r = await run(
        hook,
        { cwd: dir, session_id: "s", transcript_path: transcript, stop_hook_active: true },
        {
          tmp,
          url,
        },
      );
      assert.equal(r.out, "", hook);
    }
    assert.deepEqual(seen, []);
  });
});

test("blast radius and file context: 401 and a hanging server add nothing to the edit", async () => {
  for (const status of [401, null]) {
    await withServer(status, async (url, seen, arrived) => {
      const { dir, tmp } = dirtySession();
      const edit = {
        tool_name: "Edit",
        tool_input: { file_path: path.join(dir, "src/a.cpp") },
        cwd: dir,
        session_id: "s",
      };
      for (const hook of ["loom-blast-radius.mjs", "loom-file-context.mjs"]) {
        const op = hook === "loom-blast-radius.mjs" ? "/v1/code.impact" : "/v1/memory.for_file";
        // As for the Stop gates: a run whose git calls timed out never asked.
        let r;
        for (let attempt = 0; attempt < 3; attempt++) {
          arrived.delete(op);
          r = await run(hook, edit, { tmp, url, env: { LOOM_HOOK_TIMEOUT_MS: "500", LOOM_HOOK_EXIT_MS: "500" } });
          assert.equal(r.code, 0, `${hook} ${status}`);
          assert.equal(r.out, "", `${hook} ${status}`);
          if (arrived.has(op)) break;
        }
        // From the hook's request to its exit, as for the Stop gates.
        assert.ok(arrived.has(op), `${hook} ${status}: never sent ${op} in three runs`);
        const after = r.closedAt - arrived.get(op);
        assert.ok(after < 1500, `${hook} ${status}: ${after} ms after its request (${r.ms} ms in all)`);
        assert.ok(r.ms < 7000, `${hook} ${status} took ${r.ms} ms`);
      }
    });
  }
});

test("shunt: the whole hook ends by its deadline even when one answer is slower than it", async () => {
  const dir = checkout();
  await withServer(null, async (url, seen) => {
    const tmp = freshTmp();
    seedIndexed(dir, tmp);
    const r = await run(
      "loom-code-shunt.mjs",
      { tool_name: "Bash", tool_input: { command: 'gh issue list --search "merge hangs"' }, cwd: dir, session_id: "s" },
      { tmp, url, env: { LOOM_SHUNT_RESOLVE_TIMEOUT_MS: "1000", LOOM_TRACKER_SHUNT_TIMEOUT_MS: "20000" } },
    );
    assert.equal(r.code, 0);
    assert.equal(r.out, "", "the search runs");
    assert.deepEqual(seen, ["/v1/tracker.duplicate_check"]);
    assert.ok(r.ms < 8000, `took ${r.ms} ms`);
  });
});

test("shunt: a tracker search reaches tracker.duplicate_check redacted", async () => {
  const dir = checkout();
  const tmp = freshTmp();
  seedIndexed(dir, tmp);
  const bodies = await withCapturingServer(async (url) => {
    const command = 'gh issue list --search "login fails password=hunter2"';
    await run(
      "loom-code-shunt.mjs",
      { tool_name: "Bash", tool_input: { command }, cwd: dir, session_id: "s" },
      { tmp, url },
    );
  });
  const check = bodies.find((b) => b.url === "/v1/tracker.duplicate_check");
  assert.ok(check, JSON.stringify(bodies.map((b) => b.url)));
  assert.equal(check.body.title, "login fails password=[redacted]");
});

test("blast radius and test gate: only the diff's paths and hunk headers reach the server", async () => {
  const { dir, tmp, transcript } = dirtySession();
  fs.appendFileSync(path.join(dir, "src/a.cpp"), 'const char* api_key = "s3cr3t-value";\n');
  // a second change three lines above the first: two hunks unless they are merged
  const src = path.join(dir, "src/a.cpp");
  fs.writeFileSync(
    src,
    fs.readFileSync(src, "utf8").replace("int f397() { return 397; }", "int f397() { return -397; }"),
  );
  // A user's diff settings (colour, an external tool, a textconv filter, other
  // path prefixes) must not change what is sent. Each one, left on, changes or
  // empties the +++ path or the @@ header asserted below.
  const external = path.join(tmp, "ext-diff.sh");
  fs.writeFileSync(external, "#!/bin/sh\necho external-diff-output\n", { mode: 0o755 });
  const textconv = path.join(tmp, "textconv.sh");
  fs.writeFileSync(textconv, "#!/bin/sh\necho decrypted-by-textconv\n", { mode: 0o755 });
  fs.writeFileSync(path.join(dir, ".gitattributes"), "*.cpp diff=conv\n");
  for (const [key, value] of [
    ["color.diff", "always"],
    ["diff.external", external],
    ["diff.conv.textconv", textconv],
    ["diff.mnemonicPrefix", "true"],
    ["diff.noprefix", "true"],
    ["diff.interHunkContext", "10"],
  ]) {
    execFileSync("git", ["config", key, value], { cwd: dir });
  }
  const bodies = await withCapturingServer(async (url) => {
    const edit = {
      tool_name: "Edit",
      tool_input: { file_path: path.join(dir, "src/a.cpp") },
      cwd: dir,
      session_id: "s",
    };
    await run("loom-blast-radius.mjs", edit, { tmp, url });
    await run("loom-test-gate.mjs", { cwd: dir, session_id: "s", transcript_path: transcript }, { tmp, url });
  });
  for (const op of ["/v1/code.impact", "/v1/code.test_gate"]) {
    const sent = bodies.find((b) => b.url === op);
    assert.ok(sent, `${op} not called: ${JSON.stringify(bodies.map((b) => b.url))}`);
    assert.ok(!sent.body.diff.includes("s3cr3t-value"), op);
    assert.ok(!sent.body.diff.includes("decrypted-by-textconv"), op);
    assert.ok(!sent.body.diff.includes("int g()"), `${op} sends no changed line`);
    assert.match(sent.body.diff, /^\+\+\+ b\/src\/a\.cpp$/m, `${op} keeps the path`);
    assert.match(sent.body.diff, /^@@ .*\+\d+/m, `${op} keeps the hunk header`);
    assert.equal(sent.body.diff.match(/^@@ /gm).length, 2, `${op} keeps the two hunks apart`);
  }
});

test("blast radius: a change with no path or range for the server sends nothing", async () => {
  const { dir, tmp } = dirtySession();
  execFileSync("git", ["checkout", "-q", "--", "src/a.cpp"], { cwd: dir });
  fs.chmodSync(path.join(dir, "src/a.cpp"), 0o755);
  const bodies = await withCapturingServer(async (url) => {
    const edit = {
      tool_name: "Edit",
      tool_input: { file_path: path.join(dir, "src/a.cpp") },
      cwd: dir,
      session_id: "s",
    };
    await run("loom-blast-radius.mjs", edit, { tmp, url });
  });
  assert.deepEqual(
    bodies.map((b) => b.url).filter((u) => u === "/v1/code.impact"),
    [],
  );
});
