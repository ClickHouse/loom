// The shunt always answers from the index unless
// the developer chose local-first. A throwaway git checkout, the repo entry
// seeded in the shared cache (no code.repos call), the hook run as Claude
// Code runs it, and a local server only where a call is expected.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { cachePathFor } from "../lib/repo-scope.mjs";
import { sessionStatePath, tmpStatePath } from "../lib/tmp-state.mjs";

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
function seed(root, { asOf = FAR_BEHIND } = {}) {
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
  return tmp;
}

function runHook(payload, { tmp, url = "http://127.0.0.1:9", env = {} }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [HOOK, "PreToolUse"], {
      env: {
        PATH: process.env.PATH,
        HOME: tmp,
        TMPDIR: tmp,
        LOOM_BASE_URL: url,
        LOOM_API_KEY: "t",
        LOOM_SHUNT_REPORT: "off",
        ...env,
      },
      stdio: ["pipe", "pipe", "ignore"],
    });
    let out = "";
    child.stdout.on("data", (c) => {
      out += c;
    });
    child.on("close", () => {
      let log = "";
      try {
        log = fs.readFileSync(path.join(tmp, "loom-shunt.log"), "utf8");
      } catch {
        /* no decision logged */
      }
      const hso = out ? JSON.parse(out).hookSpecificOutput : {};
      resolve({ deny: hso.permissionDecisionReason ?? null, updatedInput: hso.updatedInput ?? null, log });
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

async function withServer(answers, fn) {
  const seen = [];
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => {
      raw += c;
    });
    req.on("end", () => {
      seen.push({ url: req.url, body: raw ? JSON.parse(raw) : {} });
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify(answers[req.url] || {}));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen);
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
  const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir) });
  assert.match(
    r.deny,
    /^loom-code-shunt: src\/a\.cpp is 900 lines and is in loom's code index \(as of 0123456789ab\)\./,
  );
  assert.match(r.log, /\thit\tRead\t/);
});

test("local_first stands the shunt down entirely, even when HEAD is the indexed commit", async () => {
  const { dir, head } = checkout({ "src/a.cpp": BIG });
  for (const env of [{ LOOM_CODE_LOCAL_FIRST: "on" }, { CLAUDE_PLUGIN_OPTION_LOCAL_FIRST: "true" }]) {
    const tmp = seed(dir, { asOf: head });
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
  const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir) });
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
    { tmp: seed(dir) },
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
      { tmp: seed(dir) },
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
      { tmp: seed(dir) },
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
    const r = await runHook({ tool_name: "Grep", tool_input, cwd: dir, session_id: "lines-no" }, { tmp: seed(dir) });
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
  const r = await withServer(answers, (url) => runHook(cmd, { tmp: seed(dir), url }));
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
  const tmp = seed(dir);
  const bash = (command) => ({ tool_name: "Bash", tool_input: { command }, cwd: dir, session_id: "e2e" });
  const r = await runHook(bash(`cd ${dir}; wc -l src/a.cpp; cat src/small.cpp; cat src/a.cpp`), { tmp });
  assert.match(r.deny, /^loom-code-shunt: src\/a\.cpp is 900 lines.*`cat` of a whole file is/);
  const lines = r.log.trim().split("\n");
  assert.equal(lines.length, 2, "the small read passed and was counted, the big one denied");
  assert.match(lines[0], /\tpass\tBash:cat\t.*\tsrc\/small\.cpp\t/);
  assert.match(lines[1], /\thit\tBash:cat\t/);
  const p = await runHook(bash("cd src && sed -n '1,40p' a.cpp; echo ---"), { tmp: seed(dir) });
  assert.equal(p.deny, null, "a ranged read passes (no read cap set)");
  assert.match(p.log, /\tpass\tBash:sed\t.*targeted_span\tsrc\/a\.cpp\t1\t40/);
});

// The hook's per-user pending batch name.
const pendingPath = (tmp) =>
  tmpStatePath("shunt-pending", typeof process.getuid === "function" ? process.getuid() : "", { ext: ".log", tmp });
const reporting = { LOOM_SHUNT_REPORT: "on", LOOM_SHUNT_READ_CAP: "200" };
const rangedRead = (dir) => ({
  ...readOf(dir, "src/a.cpp"),
  tool_input: { file_path: path.join(dir, "src/a.cpp"), offset: 1, limit: 500 },
});
const settle = () => new Promise((r) => setTimeout(r, 200));

test("a deny flushes the pending batch as code.outcome and empties it", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  await withServer({}, async (url, seen) => {
    const r = await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting });
    assert.ok(r.deny);
    await settle();
    const posts = seen.filter((s) => s.url === "/v1/code.outcome");
    assert.ok(posts.length >= 1);
    assert.equal(posts[0].body.action, "shunt_hit");
  });
  assert.equal(fs.readFileSync(pendingPath(tmp), "utf8"), "");
  assert.match(fs.readFileSync(path.join(tmp, "loom-shunt.log"), "utf8"), /\thit\tRead\t/);
});

test("the read cap denies a file twice per session, then passes it as cap_spent", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  const env = { LOOM_SHUNT_READ_CAP: "200" };
  for (let i = 0; i < 2; i++) assert.ok((await runHook(rangedRead(dir), { tmp, env })).deny, `denial ${i + 1}`);
  const third = await runHook(rangedRead(dir), { tmp, env });
  assert.equal(third.deny, null);
  assert.match(third.log, /\tpass\tRead\t\d+\t\tcap_spent\t/);
});

test("symlinks planted at the shunt's tmp files are not followed: no write, no truncation, no post", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  const plantedBatch = "1\thit\tRead\t999\t\t\t\tplanted\n";
  const planted = {
    log: path.join(tmp, "loom-shunt.log"),
    pending: pendingPath(tmp),
    cap: sessionStatePath("shunt-cap", "e2e", { tmp }),
  };
  const victims = {};
  for (const [name, at] of Object.entries(planted)) {
    victims[name] = path.join(tmp, `victim-${name}`);
    // A pending batch someone else planted: it must never reach code.outcome.
    fs.writeFileSync(victims[name], name === "pending" ? plantedBatch : "keep");
    fs.symlinkSync(victims[name], at);
  }
  await withServer({}, async (url, seen) => {
    assert.ok((await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting })).deny);
    assert.ok((await runHook(rangedRead(dir), { tmp, url, env: reporting })).deny);
    await settle();
    assert.equal(seen.filter((s) => s.url === "/v1/code.outcome").length, 0);
  });
  for (const [name, at] of Object.entries(planted)) {
    assert.equal(fs.readFileSync(victims[name], "utf8"), name === "pending" ? plantedBatch : "keep", name);
    assert.ok(fs.lstatSync(at).isSymbolicLink(), name);
  }
});
