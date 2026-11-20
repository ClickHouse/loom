// The shunt always answers from the index unless
// the developer chose local-first. A throwaway git checkout, the repo entry
// seeded in the shared cache (no code.repos call), the hook run as Claude
// Code runs it, and a local server only where a call is expected.
import "./temp-home.mjs";
import { establishFixtureSession } from "./established-session.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { cachePathFor } from "../lib/repo-scope.mjs";
import { batchKind, parseShuntBatch } from "../lib/shunt-ledger.mjs";
import { protectSession, sessionIsProtected } from "../lib/session-protection.mjs";
import { sessionStatePath, tmpStatePath } from "../lib/tmp-state.mjs";
import { hostEnv } from "./host-env.mjs";

// The hook's per-user decision log.
const logPath = (tmp) =>
  tmpStatePath("shunt-log", typeof process.getuid === "function" ? process.getuid() : "", { ext: ".log", tmp });

const HOOK = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "hooks", "loom-code-shunt.mjs");
const BIG = Array.from({ length: 900 }, (_, i) => `int f${i}() { return ${i}; }`).join("\n") + "\n";
const FAR_BEHIND = "0123456789abcdef0123456789abcdef01234567"; // not this checkout's HEAD

function checkout(files) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-shunt-e2e-")));
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true });
    fs.writeFileSync(path.join(dir, rel), text);
  }
  const g = (...a) =>
    execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", ...a], {
      cwd: dir,
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  g("init", "-q");
  g("add", ".");
  g("commit", "-qm", "c");
  return { dir, head: g("rev-parse", "HEAD") };
}

/** A fresh TMPDIR holding the repo entry the session-start primer would have
 *  written. */
function seed(root, { asOf = FAR_BEHIND, existingSessions = [] } = {}) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-shunt-tmp-")));
  fs.writeFileSync(
    cachePathFor(root, tmp),
    JSON.stringify({
      ts: Date.now(),
      indexed: true,
      org: "o",
      namespace: "code-x",
      repo: "x/y",
      branch: "main",
      prefixes: [],
      asOf,
      fetchedAt: "",
    }),
  );
  for (const sessionId of existingSessions) establishFixtureSession(tmp, sessionId);
  return tmp;
}

function recordFixtureEvent(observation, event, details = {}) {
  if (!observation || observation.events.length >= 32) return;
  observation.events.push({ event, elapsedMs: Math.round(performance.now() - observation.startedAt), ...details });
}

function observedFailure(error, observation) {
  error.message += `; shunt fixture observation ${JSON.stringify(observation.events)}`;
  return error;
}

function observedClaimControl(file) {
  const unavailable = { readable: false, controlValid: null, rows: null, roots: null };
  if (typeof process.getuid !== "function" || !fs.constants.O_NOFOLLOW || !fs.constants.O_NONBLOCK) {
    return unavailable;
  }
  let fd;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const held = fs.fstatSync(fd);
    if (
      !held.isFile() ||
      held.nlink !== 1 ||
      held.uid !== process.getuid() ||
      (held.mode & 0o077) !== 0 ||
      !Number.isSafeInteger(held.size) ||
      held.size < 0 ||
      held.size > 65536
    ) {
      return unavailable;
    }
    const bytes = Buffer.alloc(held.size);
    if (fs.readSync(fd, bytes, 0, bytes.length, 0) !== bytes.length) return unavailable;
    const current = fs.fstatSync(fd);
    if (current.size !== held.size || current.nlink !== 1) return unavailable;
    const batch = parseShuntBatch(bytes.toString("utf8"));
    return {
      readable: true,
      controlValid: Boolean(batch),
      rows: batch?.lines ?? null,
      roots: batch?.roots.length ?? null,
    };
  } catch {
    return unavailable;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function observedRetainedClaims(tmp) {
  const claims = [];
  let directory;
  let complete = false;
  try {
    directory = fs.opendirSync(tmp);
    for (let scanned = 0; scanned < 64 && claims.length < 4; scanned++) {
      const entry = directory.readSync();
      if (!entry) {
        complete = true;
        break;
      }
      if (entry.name.startsWith("loom-shunt-protected-claim-") && entry.name.endsWith(".log.control")) {
        claims.push(observedClaimControl(path.join(tmp, entry.name)));
      }
    }
    return { limited: !complete, claims };
  } finally {
    if (directory) directory.closeSync();
  }
}

function runHook(payload, { tmp, url = "http://127.0.0.1:9", env = {}, observation }) {
  const startedAt = performance.now();
  const childNumber = observation ? ++observation.children : 0;
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, "PreToolUse"], {
      env: hostEnv({
        PATH: process.env.PATH,
        HOME: tmp,
        TMPDIR: tmp,
        LOOM_BASE_URL: url,
        LOOM_API_KEY: "t",
        LOOM_SHUNT_REPORT: "off",
        ...env,
      }),
      stdio: ["pipe", "pipe", "ignore"],
    });
    let out = "";
    child.stdout.on("data", (c) => {
      out += c;
    });
    child.on("close", (code, signal) => {
      recordFixtureEvent(observation, "hook-close", {
        child: childNumber,
        code: Number.isInteger(code) && code >= -2147483648 && code <= 2147483647 ? code : null,
        signal: typeof signal === "string" && /^SIG[A-Z0-9]{1,12}$/.test(signal) ? signal : null,
        childElapsedMs: Math.round(performance.now() - startedAt),
      });
      let log = "";
      try {
        log = fs.readFileSync(logPath(tmp), "utf8");
      } catch {
        /* no decision logged */
      }
      const hso = out ? JSON.parse(out).hookSpecificOutput : {};
      resolve({
        code,
        stdout: out,
        deny: hso.permissionDecisionReason ?? null,
        updatedInput: hso.updatedInput ?? null,
        log,
      });
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

async function withServer(answers, fn) {
  const seen = [];
  const observation = { startedAt: performance.now(), children: 0, requests: 0, events: [] };
  const server = http.createServer((req, res) => {
    const request = ++observation.requests;
    recordFixtureEvent(observation, "request-open", { request });
    res.once("finish", () => recordFixtureEvent(observation, "response-finish", { request }));
    let raw = "";
    req.on("data", (c) => {
      raw += c;
    });
    req.on("end", () => {
      recordFixtureEvent(observation, "request-end", { request });
      seen.push({ url: req.url, body: raw ? JSON.parse(raw) : {} });
      res.setHeader("content-type", "application/json");
      const answer = answers[req.url];
      res.end(JSON.stringify(typeof answer === "function" ? answer() : answer || {}));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen, observation);
  } finally {
    server.close();
  }
}

const readOf = (dir, rel) => ({
  tool_name: "Read",
  tool_input: { file_path: path.join(dir, rel) },
  cwd: dir,
  session_id: "e2e",
});

test("a checkout far behind the index is still shunted", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir, { existingSessions: ["e2e"] }) });
  assert.match(
    r.deny,
    /^loom-code-shunt: src\/a\.cpp is 900 lines and is in loom's code index \(as of 0123456789ab\)\./,
  );
  assert.match(r.log, /\thit\tRead\t/);
});

test("local_first stands the shunt down entirely, even when HEAD is the indexed commit", async () => {
  const { dir, head } = checkout({ "src/a.cpp": BIG });
  for (const env of [{ LOOM_CODE_LOCAL_FIRST: "on" }, { CLAUDE_PLUGIN_OPTION_LOCAL_FIRST: "true" }]) {
    const tmp = seed(dir, { asOf: head, existingSessions: ["e2e"] });
    const r = await runHook(readOf(dir, "src/a.cpp"), { tmp, env });
    assert.equal(r.deny, null);
    assert.equal(r.log, "", "no decision at all");
    const g = await runHook(
      { tool_name: "Grep", tool_input: { pattern: "f1", path: dir }, cwd: dir, session_id: "e2e" },
      { tmp, env },
    );
    assert.equal(g.deny, null);
  }
});

test("a modified file still passes: the index cannot describe uncommitted edits", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  fs.appendFileSync(path.join(dir, "src/a.cpp"), "// mine\n");
  const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir, { existingSessions: ["e2e"] }) });
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tRead\t\d+\t\tmodified\t/);
});

test("a tree-wide grep for matching lines runs: it costs less than its redirect", async () => {
  const { dir } = checkout({ "src/a.cpp": "LOGICAL_ERROR\n" });
  const r = await runHook(
    {
      tool_name: "Grep",
      tool_input: { pattern: "Cannot allocate block number", path: path.join(dir, "src"), output_mode: "content" },
      cwd: dir,
      session_id: "e2e",
    },
    { tmp: seed(dir, { existingSessions: ["e2e"] }) },
  );
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tGrep\t\d+\t\ttree_content_grep\t/);
});

test("a tree-wide grep that lists files (Claude Code's default) passes", async () => {
  const { dir } = checkout({ "src/a.cpp": "LOGICAL_ERROR\n" });
  for (const output_mode of [undefined, "files_with_matches", "count"]) {
    const r = await runHook(
      {
        tool_name: "Grep",
        tool_input: { pattern: "Cannot allocate block number", path: path.join(dir, "src"), output_mode },
        cwd: dir,
        session_id: `e2e-${output_mode}`,
      },
      { tmp: seed(dir, { existingSessions: [`e2e-${output_mode}`] }) },
    );
    assert.equal(r.deny, null, String(output_mode));
    assert.match(r.log, /\tpass\tGrep\t\d+\t\tfile_list_grep\t/);
  }
});

test("a one-file grep asking for line numbers or context runs as content", async () => {
  const { dir } = checkout({ "src/a.cpp": "busy_timeout\n" });
  const file = path.join(dir, "src/a.cpp");
  for (const flags of [{ "-n": true }, { "-C": 3 }, { context: 2 }]) {
    const tool_input = { pattern: "busy_timeout|max_data_size", path: file, ...flags };
    const r = await runHook(
      { tool_name: "Grep", tool_input, cwd: dir, session_id: `lines-${Object.keys(flags)[0]}` },
      { tmp: seed(dir, { existingSessions: [`lines-${Object.keys(flags)[0]}`] }) },
    );
    assert.equal(r.deny, null);
    assert.deepEqual(r.updatedInput, { ...tool_input, output_mode: "content" });
    assert.match(r.log, /\tpass\tGrep\t\d+\t\tfile_grep_lines\t/);
  }
  for (const tool_input of [
    { pattern: "busy_timeout", path: file },
    { pattern: "busy_timeout", path: file, "-n": true, output_mode: "count" },
    { pattern: "busy_timeout", path: path.join(dir, "src"), "-n": true },
  ]) {
    const r = await runHook(
      { tool_name: "Grep", tool_input, cwd: dir, session_id: "lines-no" },
      { tmp: seed(dir, { existingSessions: ["lines-no"] }) },
    );
    assert.equal(r.updatedInput, null, JSON.stringify(tool_input));
  }
});

test("a symbol grep is answered by the index; a named file you modified gets its own lines inlined", async () => {
  const { dir } = checkout({ "src/a.h": "bool renameTempPartAndAdd(\n", "src/a.cpp": "// def\n" });
  fs.appendFileSync(path.join(dir, "src/a.h"), "bool renameTempPartAndAdd(int);\n");
  const answers = {
    "/v1/code.symbol": {
      definitions: [
        {
          path: "src/a.cpp",
          start_line: 7327,
          end_line: 7344,
          symbol: "DB::MergeTreeData::renameTempPartAndAdd",
          tier: "scip",
        },
      ],
    },
  };
  const cmd = {
    tool_name: "Bash",
    tool_input: { command: "grep -n renameTempPartAndAdd src/a.cpp src/a.h" },
    cwd: dir,
    session_id: "e2e",
  };
  const r = await withServer(answers, (url) => runHook(cmd, { tmp: seed(dir, { existingSessions: ["e2e"] }), url }));
  assert.match(
    r.deny,
    /Index answer: DB::MergeTreeData::renameTempPartAndAdd is defined at src\/a\.cpp:7327-7344 \(code-x @ 012345678, scip tier\)\./,
  );
  assert.match(
    r.deny,
    /You have local changes in src\/a\.h; your lines from there follow, so no grep is needed:\ngrep -n "renameTempPartAndAdd" src\/a\.h:\n1:bool renameTempPartAndAdd\(\n2:bool renameTempPartAndAdd\(int\);/,
  );
  assert.doesNotMatch(r.deny, /HEAD|ANSWER FROM THE INDEX|do not grep/);
});

test("a chained Bash read is judged segment by segment; the first deny wins", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG, "src/small.cpp": "int x;\n" });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const bash = (command) => ({ tool_name: "Bash", tool_input: { command }, cwd: dir, session_id: "e2e" });
  // As a Git Bash command names it on Windows: forward slashes.
  const cdDir = dir.replace(/\\/g, "/");
  const r = await runHook(bash(`cd ${cdDir}; wc -l src/a.cpp; cat src/small.cpp; cat src/a.cpp`), { tmp });
  assert.match(r.deny, /^loom-code-shunt: src\/a\.cpp is 900 lines.*`cat` of a whole file is/);
  const lines = r.log.trim().split("\n");
  assert.equal(lines.length, 2, "the small read passed and was counted, the big one denied");
  assert.match(lines[0], /\tpass\tBash:cat\t.*\tsrc\/small\.cpp\t/);
  assert.match(lines[1], /\thit\tBash:cat\t/);
  const p = await runHook(bash("cd src && sed -n '1,40p' a.cpp; echo ---"), {
    tmp: seed(dir, { existingSessions: ["e2e"] }),
  });
  assert.equal(p.deny, null, "a ranged read passes (no read cap set)");
  assert.match(p.log, /\tpass\tBash:sed\t.*targeted_span\tsrc\/a\.cpp\t1\t40/);
});

// The hook's pending batch for the seeded repository (o/code-x), the server at
// `url`, runHook's token and a session.
const pendingPath = (tmp, url, session = "e2e", namespace = "code-x") =>
  sessionStatePath(batchKind({ serverUrl: url, token: "t", org: "o", namespace }), session, {
    ext: ".log.control",
    tmp,
  });
const reporting = { LOOM_SHUNT_REPORT: "on", LOOM_SHUNT_READ_CAP: "200" };
const rangedRead = (dir) => ({
  ...readOf(dir, "src/a.cpp"),
  tool_input: { file_path: path.join(dir, "src/a.cpp"), offset: 1, limit: 500 },
});
const settle = () => new Promise((r) => setTimeout(r, 200));
const outcomes = (seen) => seen.filter((s) => s.url === "/v1/code.outcome");
const plantedBatch = "1\thit\tRead\t999\t\t\tplanted.cpp\t1\t2\n";
const carriesPlanted = (seen) => outcomes(seen).some((s) => JSON.stringify(s.body).includes("planted.cpp"));
const posix = process.platform !== "win32";

test("a deny claims the pending batch, posts it as code.outcome and removes it", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  await withServer({}, async (url, seen) => {
    const r = await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting });
    assert.ok(r.deny);
    await settle();
    assert.ok(outcomes(seen).length >= 1);
    assert.equal(outcomes(seen)[0].body.action, "shunt_hit");
    assert.equal(fs.existsSync(pendingPath(tmp, url)), false);
  });
  assert.deepEqual(
    fs.readdirSync(tmp).filter((n) => n.startsWith("loom-shunt-protected-claim-")),
    [],
    "the claimed batch is removed",
  );
  assert.match(fs.readFileSync(logPath(tmp), "utf8"), /\thit\tRead\t/);
});

test("the read cap denies a file twice per session, then passes it as cap_spent", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const env = { LOOM_SHUNT_READ_CAP: "200" };
  for (let i = 0; i < 2; i++) assert.ok((await runHook(rangedRead(dir), { tmp, env })).deny, `denial ${i + 1}`);
  const third = await runHook(rangedRead(dir), { tmp, env });
  assert.equal(third.deny, null);
  assert.match(third.log, /\tpass\tRead\t\d+\t\tcap_spent\t/);
});

test("symlinks planted at the shunt's tmp files are not followed and cannot lock the agent out", {
  skip: !posix,
}, async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const planted = {};
  const victims = {};
  await withServer({}, async (url, seen) => {
    Object.assign(planted, {
      log: logPath(tmp),
      pending: pendingPath(tmp, url),
      cap: sessionStatePath("shunt-cap", "e2e", { tmp }),
    });
    for (const [name, at] of Object.entries(planted)) {
      victims[name] = path.join(tmp, `victim-${name}`);
      // A pending batch someone else planted: it must never reach code.outcome.
      fs.writeFileSync(victims[name], name === "pending" ? plantedBatch : "keep");
      fs.symlinkSync(victims[name], at);
    }
    assert.ok((await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting })).deny);
    // The cap file cannot be kept, so the over-cap read passes instead of being denied.
    const ranged = await runHook(rangedRead(dir), { tmp, url, env: reporting });
    assert.equal(ranged.deny, null);
    await settle();
    assert.equal(carriesPlanted(seen), false, "the planted batch is never posted");
  });
  for (const [name, at] of Object.entries(planted)) {
    assert.equal(fs.readFileSync(victims[name], "utf8"), name === "pending" ? plantedBatch : "keep", name);
    if (name !== "pending") assert.ok(fs.lstatSync(at).isSymbolicLink(), name);
  }
});

test("a hard link planted at the pending batch is neither posted nor truncated", { skip: !posix }, async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const victim = path.join(tmp, "victim-log");
  fs.writeFileSync(victim, plantedBatch);
  await withServer({}, async (url, seen) => {
    fs.linkSync(victim, pendingPath(tmp, url));
    for (let i = 0; i < 2; i++) assert.ok((await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting })).deny);
    await settle();
    assert.equal(carriesPlanted(seen), false);
  });
  assert.equal(fs.readFileSync(victim, "utf8"), plantedBatch);
});

test("a flush also posts the batch another session left behind", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  fs.appendFileSync(path.join(dir, "src/a.cpp"), "// mine\n");
  const { dir: other } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["A", "B"] });
  fs.writeFileSync(cachePathFor(other, tmp), fs.readFileSync(cachePathFor(dir, tmp)));
  await withServer({}, async (url, seen) => {
    const a = await runHook({ ...readOf(dir, "src/a.cpp"), session_id: "A" }, { tmp, url, env: reporting });
    assert.equal(a.deny, null, "a modified file passes; nothing is flushed yet");
    await settle();
    assert.equal(outcomes(seen).length, 0);
    assert.ok(fs.existsSync(pendingPath(tmp, url, "A")));
    const b = await runHook({ ...readOf(other, "src/a.cpp"), session_id: "B" }, { tmp, url, env: reporting });
    assert.ok(b.deny);
    await settle();
    const passes = outcomes(seen).filter((o) => o.body.action === "shunt_pass");
    assert.deepEqual(
      passes.map((o) => o.body.source),
      ["modified"],
      "session A's pass is posted by session B's flush",
    );
    assert.equal(fs.existsSync(pendingPath(tmp, url, "A")), false);
  });
});

test("a flush leaves another reporting scope's batch alone", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  await withServer({}, async (url, seen) => {
    // Another repository's batch, and one this repository's lines would go to under another server.
    const others = [pendingPath(tmp, url, "X", "code-other"), pendingPath(tmp, "http://127.0.0.1:9", "Y")];
    for (const f of others) fs.writeFileSync(f, plantedBatch, { mode: 0o600 });
    assert.ok((await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting })).deny);
    await settle();
    assert.ok(outcomes(seen).length >= 1, "this scope's own batch is posted");
    assert.equal(carriesPlanted(seen), false);
    for (const f of others) assert.equal(fs.readFileSync(f, "utf8"), plantedBatch);
  });
});

const plantCap = (tmp, session = "e2e") => {
  const victim = path.join(tmp, "victim-cap");
  fs.writeFileSync(victim, "keep");
  fs.symlinkSync(victim, sessionStatePath("shunt-cap", session, { tmp }));
  return victim;
};

test("a symbol grep whose budget cannot be written passes as budget_unrecorded", { skip: !posix }, async () => {
  const { dir } = checkout({ "src/a.h": "bool renameTempPartAndAdd(\n", "src/a.cpp": "// def\n" });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const victim = plantCap(tmp);
  const cmd = {
    tool_name: "Bash",
    tool_input: { command: "grep -n renameTempPartAndAdd src/a.cpp src/a.h" },
    cwd: dir,
    session_id: "e2e",
  };
  const r = await withServer({}, (url) => runHook(cmd, { tmp, url }));
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tBash:grep\t\d+\t\tbudget_unrecorded\t/);
  assert.equal(fs.readFileSync(victim, "utf8"), "keep");
});

test("an answered tracker search whose budget cannot be written passes as budget_unrecorded", {
  skip: !posix,
}, async () => {
  const { dir } = checkout({ "src/a.cpp": "// x\n" });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  const victim = plantCap(tmp);
  const cmd = {
    tool_name: "Bash",
    tool_input: { command: 'gh issue list --search "merge hangs"' },
    cwd: dir,
    session_id: "e2e",
  };
  const answers = { "/v1/tracker.duplicate_check": { verdict: "none", epistemic: "calibrated", items: [] } };
  const r = await withServer(answers, async (url, seen) => {
    const out = await runHook(cmd, { tmp, url });
    assert.ok(
      seen.some((x) => x.url === "/v1/tracker.duplicate_check"),
      "the record was asked",
    );
    return out;
  });
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tBash:gh\t\d+\t\tbudget_unrecorded\t/);
  assert.equal(fs.readFileSync(victim, "utf8"), "keep");
});

test("an over-cap read whose budget cannot be written passes as budget_unrecorded", { skip: !posix }, async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["e2e"] });
  plantCap(tmp);
  const r = await runHook(rangedRead(dir), { tmp, env: { LOOM_SHUNT_READ_CAP: "200" } });
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tRead\t\d+\t\tbudget_unrecorded\t/);
});

test("a producer protected after queuing keeps its bytes while an unrelated clean hit posts", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  fs.appendFileSync(path.join(dir, "src/a.cpp"), "// private producer change\n");
  const { dir: other } = checkout({ "src/b.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["A", "B"] });
  const rootA = establishFixtureSession(tmp, "A");
  const rootB = establishFixtureSession(tmp, "B");
  fs.writeFileSync(cachePathFor(other, tmp), fs.readFileSync(cachePathFor(dir, tmp)));
  try {
    await withServer({}, async (url, seen) => {
      const a = await runHook({ ...readOf(dir, "src/a.cpp"), session_id: "A" }, { tmp, url, env: reporting });
      assert.equal(a.deny, null);
      const queuedA = pendingPath(tmp, url, "A");
      const before = fs.readFileSync(queuedA);
      assert.ok(before.length > 0);
      assert.equal(outcomes(seen).length, 0);
      protectSession(rootA);
      assert.equal(sessionIsProtected(rootA), true);
      assert.equal(sessionIsProtected(rootB), false);
      const b = await runHook({ ...readOf(other, "src/b.cpp"), session_id: "B" }, { tmp, url, env: reporting });
      assert.ok(b.deny);
      await settle();
      assert.equal(outcomes(seen).length, 1, "the clean producer's own hit is sent");
      assert.equal(outcomes(seen)[0].body.action, "shunt_hit");
      assert.ok(outcomes(seen)[0].body.spans.some((span) => span.startsWith("src/b.cpp:")));
      assert.equal(JSON.stringify(outcomes(seen)).includes("src/a.cpp"), false);
      assert.equal(
        outcomes(seen).some((entry) => entry.body.source === "modified"),
        false,
      );
      assert.deepEqual(fs.readFileSync(queuedA), before, "protected evidence is neither claimed nor changed");
      assert.equal(fs.existsSync(pendingPath(tmp, url, "B")), false);
      assert.equal(sessionIsProtected(rootB), false);
    });
  } finally {
    for (const owned of [tmp, dir, other]) fs.rmSync(owned, { recursive: true, force: true });
  }
});

test("a marked aggregate response protects every producer and retains both unacknowledged claims", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  fs.appendFileSync(path.join(dir, "src/a.cpp"), "// producer change\n");
  const { dir: other } = checkout({ "src/b.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["A", "B"] });
  const rootA = establishFixtureSession(tmp, "A");
  const rootB = establishFixtureSession(tmp, "B");
  fs.writeFileSync(cachePathFor(other, tmp), fs.readFileSync(cachePathFor(dir, tmp)));
  try {
    await withServer({ "/v1/code.outcome": { mirrored: true } }, async (url, seen, observation) => {
      let queuedA;
      try {
        const a = await runHook(
          { ...readOf(dir, "src/a.cpp"), session_id: "A" },
          { tmp, url, env: reporting, observation },
        );
        assert.equal(a.deny, null);
        queuedA = pendingPath(tmp, url, "A");
        const beforeA = fs.readFileSync(queuedA, "utf8");
        assert.equal(outcomes(seen).length, 0);
        assert.equal(sessionIsProtected(rootA), false);
        assert.equal(sessionIsProtected(rootB), false);
        const b = await runHook(
          { ...readOf(other, "src/b.cpp"), session_id: "B" },
          { tmp, url, env: reporting, observation },
        );
        assert.ok(b.deny);
        await settle();
        assert.equal(outcomes(seen).length, 1, "the first marked answer suppresses the aggregate's second body");
        assert.equal(outcomes(seen)[0].body.action, "shunt_hit");
        assert.equal(sessionIsProtected(rootA), true);
        assert.equal(sessionIsProtected(rootB), true);
        assert.equal(fs.existsSync(queuedA), false);
        assert.equal(fs.existsSync(pendingPath(tmp, url, "B")), false);
        const claims = fs.readdirSync(tmp).filter((name) => name.startsWith("loom-shunt-protected-claim-"));
        assert.equal(claims.length, 2, "claiming moves evidence; a marked answer does not acknowledge it");
        const retained = claims.map((name) => fs.readFileSync(path.join(tmp, name), "utf8"));
        assert.ok(retained.includes(beforeA), "producer A's exact queued bytes remain in its claim");
        const rows = retained.flatMap((text) =>
          text
            .trimEnd()
            .split("\n")
            .map((line) => JSON.parse(line)),
        );
        assert.equal(rows.length, 2);
        const rowA = rows.find((row) =>
          row.protectionRoots.some((root) => root.base === rootA.base && root.id === rootA.id),
        );
        const rowB = rows.find((row) =>
          row.protectionRoots.some((root) => root.base === rootB.base && root.id === rootB.id),
        );
        assert.ok(rowA && rowB, "both actual producer roots stay attached to their evidence");
        assert.match(rowA.line, /\tpass\tRead\t.*\tmodified\tsrc\/a\.cpp\t/);
        assert.match(rowB.line, /\thit\tRead\t.*\tsrc\/b\.cpp\t/);
        const scopePrefix = `loom-${batchKind({ serverUrl: url, token: "t", org: "o", namespace: "code-x" })}-`;
        assert.deepEqual(
          fs.readdirSync(tmp).filter((name) => name.startsWith(scopePrefix) && name.endsWith(".log.control")),
          [],
        );
      } catch (error) {
        try {
          const aTracked = queuedA !== undefined;
          const aExists = aTracked ? fs.existsSync(queuedA) : null;
          const names = fs.readdirSync(tmp);
          recordFixtureEvent(observation, "marked-queue-snapshot", {
            aPendingTracked: aTracked,
            aPendingExists: aExists,
            aPendingBytes: aTracked ? (aExists ? fs.statSync(queuedA).size : 0) : null,
            ownedClaimCount: names.filter((name) => name.startsWith("loom-shunt-protected-claim-")).length,
            ownedClaimPayloadCount: names.filter(
              (name) => name.startsWith("loom-shunt-protected-claim-") && name.endsWith(".log.control"),
            ).length,
          });
        } catch {
          recordFixtureEvent(observation, "snapshot-unavailable");
        }
        throw observedFailure(error, observation);
      }
    });
  } finally {
    for (const owned of [tmp, dir, other]) fs.rmSync(owned, { recursive: true, force: true });
  }
});

test("an unattributed batch in the same protected scope stays untouched while a clean hit posts", async () => {
  const { dir } = checkout({ "src/b.cpp": BIG });
  const tmp = seed(dir, { existingSessions: ["B"] });
  try {
    await withServer({}, async (url, seen, observation) => {
      try {
        const unattributed = pendingPath(tmp, url, "unknown-producer");
        fs.writeFileSync(unattributed, plantedBatch, { mode: 0o600 });
        const before = fs.readFileSync(unattributed);
        const b = await runHook(
          { ...readOf(dir, "src/b.cpp"), session_id: "B" },
          { tmp, url, env: reporting, observation },
        );
        assert.ok(b.deny);
        await settle();
        assert.equal(outcomes(seen).length, 1);
        assert.equal(outcomes(seen)[0].body.action, "shunt_hit");
        assert.ok(outcomes(seen)[0].body.spans.some((span) => span.startsWith("src/b.cpp:")));
        assert.equal(carriesPlanted(seen), false);
        assert.deepEqual(fs.readFileSync(unattributed), before, "a matching filename cannot supply producer authority");
        assert.equal(fs.existsSync(pendingPath(tmp, url, "B")), false);
        assert.deepEqual(
          fs.readdirSync(tmp).filter((name) => name.startsWith("loom-shunt-protected-claim-")),
          [],
        );
      } catch (error) {
        try {
          const pending = pendingPath(tmp, url, "B");
          const pendingExists = fs.existsSync(pending);
          recordFixtureEvent(observation, "queue-snapshot", {
            bPendingExists: pendingExists,
            bPendingBytes: pendingExists ? fs.statSync(pending).size : 0,
            ownedClaimCount: fs.readdirSync(tmp).filter((name) => name.startsWith("loom-shunt-protected-claim-"))
              .length,
            unattributedFileExists: fs.existsSync(pendingPath(tmp, url, "unknown-producer")),
            claimControl: observedRetainedClaims(tmp),
          });
        } catch {
          recordFixtureEvent(observation, "snapshot-unavailable");
        }
        throw observedFailure(error, observation);
      }
    });
  } finally {
    for (const owned of [tmp, dir]) fs.rmSync(owned, { recursive: true, force: true });
  }
});

for (const mode of ["mirrored", "late-sibling-mark"]) {
  test(`a symbol reply ${mode} withholds the actual shunt denial and decision log`, async () => {
    const { dir } = checkout({ "src/a.cpp": "bool renameTempPartAndAdd();\n" });
    const tmp = seed(dir, { existingSessions: ["e2e"] });
    const root = establishFixtureSession(tmp, "e2e");
    try {
      const answers = {
        "/v1/code.symbol": () => {
          // The real hook has dispatched and is awaiting this response.
          if (mode === "late-sibling-mark") protectSession(root);
          return {
            mirrored: mode === "mirrored",
            definitions: [
              {
                path: "src/a.cpp",
                start_line: 1,
                end_line: 1,
                symbol: "DB::MergeTreeData::renameTempPartAndAdd",
                tier: "scip",
              },
            ],
          };
        },
      };
      await withServer(answers, async (url, seen) => {
        const answer = await runHook(
          {
            tool_name: "Bash",
            tool_input: { command: "grep -n renameTempPartAndAdd src/a.cpp" },
            cwd: dir,
            session_id: "e2e",
          },
          { tmp, url },
        );
        assert.equal(answer.code, 0);
        assert.deepEqual(
          seen.map((request) => request.url),
          ["/v1/code.symbol"],
        );
        assert.equal(answer.stdout, "");
        assert.equal(answer.deny, null);
        assert.equal(answer.updatedInput, null);
        assert.equal(answer.log, "");
        assert.equal(sessionIsProtected(root), true);
      });
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}

for (const status of ["modified", "untracked"]) {
  for (const tool of ["Grep", "Bash"]) {
    test(`${status} ${tool} single-file grep stays local without indexed requests`, async (t) => {
      const { dir } = checkout({ "src/parser.py": "field_value = 1\n" });
      const rel = status === "untracked" ? "src/local_parser.py" : "src/parser.py";
      fs.writeFileSync(path.join(dir, rel), "field_value = 2\n");
      const tmp = seed(dir, { existingSessions: ["local-query"] });
      t.after(() => {
        fs.rmSync(dir, { recursive: true, force: true });
        fs.rmSync(tmp, { recursive: true, force: true });
      });
      const input =
        tool === "Grep"
          ? { pattern: "field_value", path: path.join(dir, rel), output_mode: "content" }
          : { command: `grep -n field_value ${rel}` };
      await withServer({}, async (url, seen) => {
        const result = await runHook(
          { tool_name: tool, tool_input: input, cwd: dir, session_id: "local-query" },
          { tmp, url },
        );
        assert.equal(result.deny, null);
        assert.equal(result.updatedInput, null);
        assert.match(result.log, new RegExp(`\\tpass\\t(?:Grep|Bash:grep)\\t\\d+\\t\\t${status}\\t`));
        assert.deepEqual(seen, [], "working-tree evidence needs no indexed lookup");
      });
    });
  }
}
