// The shunt always answers from the index unless
// the developer chose local-first. A throwaway git checkout, the repo entry
// seeded in the shared cache (no code.repos call), the hook run as Claude
// Code runs it, and a local server only where a call is expected: a refusal
// needs the index's answer, so every test that expects one serves it.
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
import { batchKind } from "../lib/shunt-ledger.mjs";
import { readSessionState, sessionStatePath, tmpStatePath, writeSessionState } from "../lib/tmp-state.mjs";
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
    child.on("close", () => {
      let log = "";
      try {
        log = fs.readFileSync(logPath(tmp), "utf8");
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
      const answer = answers[req.url];
      res.end(JSON.stringify((typeof answer === "function" ? answer() : answer) || {}));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  try {
    return await fn(`http://127.0.0.1:${server.address().port}`, seen);
  } finally {
    server.close();
  }
}

// The index's outline of BIG, as code.outline serves it: whole, in source order.
const OUTLINE = {
  "/v1/code.outline": {
    entries: [
      { kind: "function", qualified_name: "f0", signature: "int f0()", start_line: 1, end_line: 1, tier: "scip" },
      {
        kind: "function",
        qualified_name: "f450",
        signature: "int f450()",
        start_line: 451,
        end_line: 451,
        tier: "scip",
      },
      {
        kind: "function",
        qualified_name: "f899",
        signature: "int f899()",
        start_line: 900,
        end_line: 900,
        tier: "scip",
      },
    ],
    total: 3,
    returned: 3,
    elided: 0,
  },
};
const lookups = (seen) => seen.filter((s) => s.url !== "/v1/code.outcome").map((s) => s.url);

const readOf = (dir, rel) => ({
  tool_name: "Read",
  tool_input: { file_path: path.join(dir, rel) },
  cwd: dir,
  session_id: "e2e",
});

test("a checkout far behind the index is still shunted, with the file's declarations", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  await withServer(OUTLINE, async (url, seen) => {
    const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir), url });
    assert.match(
      r.deny,
      /^loom-code-shunt: src\/a\.cpp is 900 lines and is in loom's code index \(as of 0123456789ab\)\. The index's outline of it \(lines, qualified name, header; it lists functions, methods, classes and structs, not interfaces, type aliases, enums or constants\):\n1-1 f0 int f0\(\)\n451-451 f450 int f450\(\)\n900-900 f899 int f899\(\)\nRead the lines you need/,
    );
    // The file's tokens, less the refusal's own text.
    const kept = Math.floor(BIG.length / 4) - Math.floor(r.deny.length / 4);
    assert.match(r.log, new RegExp(`\\thit\\tRead\\t${kept}\\t`));
    assert.deepEqual(seen[0].body, {
      org: "o",
      namespace: "code-x",
      path: "src/a.cpp",
      token_budget: 4000,
      include_lessons: false,
      consumer: "claude-code-shunt",
    });
  });
});

test("a whole-file read the index cannot outline in full runs", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const cut = { "/v1/code.outline": { ...OUTLINE["/v1/code.outline"], total: 900, elided: 897 } };
  for (const [name, answers] of Object.entries({ cut, empty: { "/v1/code.outline": { entries: [] } }, silent: {} })) {
    await withServer(answers, async (url) => {
      const r = await runHook(readOf(dir, "src/a.cpp"), { tmp: seed(dir), url });
      assert.equal(r.deny, null, name);
      assert.match(r.log, /\tpass\tRead\t\d+\t\tindex_unanswered\tsrc\/a\.cpp\t/, name);
    });
  }
  const downTmp = seed(dir);
  const down = await runHook(readOf(dir, "src/a.cpp"), { tmp: downTmp });
  assert.equal(down.deny, null, "an unreachable server");
  assert.match(down.log, /\tpass\tRead\t\d+\t\tindex_unanswered\t/);
  // A lookup nothing answered counts too: the file is read for the rest of the session.
  await withServer(OUTLINE, async (url, seen) => {
    assert.equal((await runHook(readOf(dir, "src/a.cpp"), { tmp: downTmp, url })).deny, null);
    assert.deepEqual(lookups(seen), []);
  });
  // The index is asked about the file once per session.
  await withServer(cut, async (url, seen) => {
    const tmp = seed(dir);
    for (let read = 0; read < 2; read++) {
      const r = await runHook(readOf(dir, "src/a.cpp"), { tmp, url });
      assert.equal(r.deny, null);
      assert.match(r.log, /\tindex_unanswered\tsrc\/a\.cpp\t\d+\t\d+\n$/);
    }
    assert.deepEqual(lookups(seen), ["/v1/code.outline"]);
    const other = await runHook({ ...readOf(dir, "src/a.cpp"), session_id: "another" }, { tmp, url });
    assert.equal(other.deny, null);
    assert.deepEqual(lookups(seen), ["/v1/code.outline", "/v1/code.outline"], "another session asks for itself");
  });
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

const bashOf = (dir, command, session = "e2e") => ({
  tool_name: "Bash",
  tool_input: { command },
  cwd: dir,
  session_id: session,
});
const SYMBOL = {
  "/v1/code.symbol": {
    definitions: [{ path: "src/a.cpp", start_line: 7327, end_line: 7344, symbol: "DB::renameTempPartAndAdd" }],
  },
};

test("a Bash command of several parts is never refused: its reads are counted, the index is not asked", async () => {
  const { dir } = checkout({
    "src/a.cpp": BIG,
    "src/small.cpp": "int x;\n",
    "src/a.h": "bool renameTempPartAndAdd(\n",
  });
  // As a Git Bash command names it on Windows: forward slashes.
  const cdDir = dir.replace(/\\/g, "/");
  const env = { LOOM_SHUNT_READ_CAP: "200" };
  await withServer({ ...OUTLINE, ...SYMBOL }, async (url, seen) => {
    const tmp = seed(dir);
    const r = await runHook(bashOf(dir, `cd ${cdDir}; wc -l src/a.cpp; cat src/small.cpp; cat src/a.cpp`), {
      tmp,
      url,
    });
    assert.equal(r.deny, null);
    const lines = r.log.trim().split("\n");
    assert.equal(lines.length, 2, "both reads are counted");
    assert.match(lines[0], /\tpass\tBash:cat\t\d+\t\tunder_max_lines\tsrc\/small\.cpp\t/);
    assert.match(lines[1], /\tpass\tBash:cat\t\d+\t\tcompound_command\tsrc\/a\.cpp\t/);
    for (const [command, logged] of [
      ["cat src/a.cpp | head -5; cat src/a.cpp && echo ok", /\tpass\tBash:cat\t\d+\t\tcompound_command\t/],
      ["make test || cat src/a.cpp", /\tpass\tBash:cat\t\d+\t\tcompound_command\t/],
      ["cd src && sed -n '1,500p' a.cpp", /\tpass\tBash:sed\t\d+\t\tcompound_command\tsrc\/a\.cpp\t1\t500/],
      ["cd src && sed -n '1,40p' a.cpp; echo ---", /\tpass\tBash:sed\t\d+\t\ttargeted_span\tsrc\/a\.cpp\t1\t40/],
      ["git status; grep -n renameTempPartAndAdd src/a.h", /\tpass\tBash:grep\t\d+\t\tcompound_command\tsrc\/a\.h\t/],
      ["git status; grep -n TODO src/a.h", /\tpass\tBash:grep\t\d+\t\tsingle_file_grep\tsrc\/a\.h\t/],
      ['cd src; gh issue list --search "merge hangs"', null],
    ]) {
      const p = await runHook(bashOf(dir, command), { tmp: seed(dir), url, env });
      assert.equal(p.deny, null, command);
      if (logged) assert.match(p.log, logged, command);
      else assert.equal(p.log, "", command);
    }
    assert.deepEqual(lookups(seen), [], "a pass costs no lookup");
    // The same reads and the same grep, each as the whole command, are refused with the index's answer.
    const cat = await runHook(bashOf(dir, "cat src/a.cpp"), { tmp: seed(dir), url });
    assert.match(
      cat.deny,
      /^loom-code-shunt: src\/a\.cpp is 900 lines.*The index's outline of it.*:\n1-1 f0 int f0\(\)\n/,
    );
    assert.match(cat.log, /\thit\tBash:cat\t/);
    const sed = await runHook(bashOf(dir, "sed -n '400,899p' src/a.cpp"), { tmp: seed(dir), url, env });
    assert.match(
      sed.deny,
      /^loom-code-shunt: `sed` of src\/a\.cpp lines 400-899 is 500 lines, over the 200-line read window.*The index's outline of those lines \(lines, qualified name, header; [^)]*\):\n451-451 f450 int f450\(\)\nRead the ones you need/,
    );
    const grep = await runHook(bashOf(dir, "grep -n renameTempPartAndAdd src/a.h"), { tmp: seed(dir), url });
    assert.match(grep.deny, /Index answer: DB::renameTempPartAndAdd is defined at src\/a\.cpp:7327-7344/);
  });
});

test("a ranged read, a grep for a local variable and a grep for test names in one command all run", async () => {
  const { dir } = checkout({
    "src/app/blueprints.py": Array.from({ length: 622 }, (_, i) => `x${i} = ${i}`).join("\n") + "\n",
    "tests/test_blueprints.py": "def test_dotted_name_not_allowed(app, client):\n    pass\n",
  });
  const command =
    'sed -n 188,198p src/app/blueprints.py; grep -n "self_name" src/app/blueprints.py; ' +
    'grep -n "def test_dotted_name\\|def test_.*dot" tests/test_blueprints.py';
  await withServer({}, async (url, seen) => {
    const tmp = seed(dir);
    const r = await runHook(bashOf(dir, command), { tmp, url });
    assert.equal(r.deny, null);
    const lines = r.log.trim().split("\n");
    assert.equal(lines.length, 3);
    assert.match(lines[0], /\tpass\tBash:sed\t\d+\t\ttargeted_span\tsrc\/app\/blueprints\.py\t188\t198/);
    assert.match(lines[1], /\tpass\tBash:grep\t\d+\t\tcompound_command\tsrc\/app\/blueprints\.py\t/);
    assert.match(lines[2], /\tpass\tBash:grep\t\d+\t\tsingle_file_grep\ttests\/test_blueprints\.py\t/);
    assert.deepEqual(lookups(seen), []);
    // Alone, the grep asks the index once: it holds no definition of a local variable, so the grep runs.
    const alone = bashOf(dir, 'grep -n "self_name" src/app/blueprints.py');
    const first = await runHook(alone, { tmp, url });
    assert.equal(first.deny, null);
    assert.match(first.log, /\tpass\tBash:grep\t\d+\t\tindex_unanswered\tsrc\/app\/blueprints\.py\t\d+\t\d+\n$/);
    const again = await runHook(alone, { tmp, url });
    assert.equal(again.deny, null);
    assert.match(again.log, /\tsymbol_grep_spent\tsrc\/app\/blueprints\.py\t\d+\t\d+\n$/);
    assert.deepEqual(lookups(seen), ["/v1/code.symbol"], "one lookup per symbol per session");
  });
});

test("a symbol grep the index answers with several definitions, or with local lines that do not fit, runs", async () => {
  const many = Array.from({ length: 20 }, () => "bool renameTempPartAndAdd(int);").join("\n") + "\n";
  const { dir } = checkout({ "src/a.h": "bool renameTempPartAndAdd(\n", "src/a.cpp": "// def\n" });
  const one = SYMBOL["/v1/code.symbol"].definitions[0];
  const grep = (session) => ({
    tool_name: "Grep",
    tool_input: { pattern: "renameTempPartAndAdd", path: path.join(dir, "src/a.cpp") },
    cwd: dir,
    session_id: session,
  });
  await withServer({ "/v1/code.symbol": { definitions: [one, { ...one, path: "src/a.h" }] } }, async (url) => {
    const r = await runHook(grep("two"), { tmp: seed(dir), url });
    assert.equal(r.deny, null);
    assert.match(r.log, /\tpass\tGrep\t\d+\t\tindex_unanswered\t/);
  });
  await withServer(SYMBOL, async (url) => {
    assert.match((await runHook(grep("one"), { tmp: seed(dir), url })).deny, /Index answer: DB::renameTempPartAndAdd/);
    fs.appendFileSync(path.join(dir, "src/a.h"), many);
    const r = await runHook(bashOf(dir, "grep -n renameTempPartAndAdd src/a.cpp src/a.h", "long"), {
      tmp: seed(dir),
      url,
    });
    assert.equal(r.deny, null, "21 local lines are more than a refusal carries");
    assert.match(r.log, /\tpass\tBash:grep\t\d+\t\tindex_unanswered\t/);
  });
});

// The hook's pending batch for the seeded repository (o/code-x), the server at
// `url`, runHook's token and a session.
const pendingPath = (tmp, url, session = "e2e", namespace = "code-x") =>
  sessionStatePath(batchKind({ serverUrl: url, token: "t", org: "o", namespace }), session, { ext: ".log", tmp });
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
  const tmp = seed(dir);
  await withServer(OUTLINE, async (url, seen) => {
    const r = await runHook(readOf(dir, "src/a.cpp"), { tmp, url, env: reporting });
    assert.ok(r.deny);
    await settle();
    assert.ok(outcomes(seen).length >= 1);
    assert.equal(outcomes(seen)[0].body.action, "shunt_hit");
    assert.equal(fs.existsSync(pendingPath(tmp, url)), false);
  });
  assert.deepEqual(
    fs.readdirSync(tmp).filter((n) => n.startsWith("loom-shunt-claim-")),
    [],
    "the claimed batch is removed",
  );
  assert.match(fs.readFileSync(logPath(tmp), "utf8"), /\thit\tRead\t/);
});

test("the read cap denies a file twice per session, then passes it as cap_spent", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  const env = { LOOM_SHUNT_READ_CAP: "200" };
  await withServer(OUTLINE, async (url, seen) => {
    for (let i = 0; i < 2; i++) {
      const r = await runHook(rangedRead(dir), { tmp, url, env });
      assert.match(
        r.deny,
        /lines 1-500 is 500 lines.*:\n1-1 f0 int f0\(\)\n451-451 f450 int f450\(\)\nRead the ones/,
        `denial ${i + 1}`,
      );
    }
    const third = await runHook(rangedRead(dir), { tmp, url, env });
    assert.equal(third.deny, null);
    assert.match(third.log, /\tpass\tRead\t\d+\t\tcap_spent\t/);
    assert.equal(lookups(seen).length, 2, "a spent budget asks nothing");
  });
  // An over-cap read the index cannot describe runs, and spends none of the file's denials.
  const quiet = seed(dir);
  await withServer({}, async (url) => {
    const r = await runHook(rangedRead(dir), { tmp: quiet, url, env });
    assert.equal(r.deny, null);
    assert.match(r.log, /\tpass\tRead\t\d+\t\tindex_unanswered\tsrc\/a\.cpp\t1\t500/);
  });
  await withServer(OUTLINE, async (url) => {
    assert.ok((await runHook(rangedRead(dir), { tmp: quiet, url, env })).deny, "the budget is still whole");
  });
});

test("a tail slice over the read cap is refused with the declarations of the file's last lines", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const cmd = { tool_name: "Bash", tool_input: { command: "tail -n 300 src/a.cpp" }, cwd: dir, session_id: "e2e" };
  await withServer(OUTLINE, async (url) => {
    const r = await runHook(cmd, { tmp: seed(dir), url, env: { LOOM_SHUNT_READ_CAP: "200" } });
    assert.match(
      r.deny,
      /^loom-code-shunt: `tail` of src\/a\.cpp lines 601-900 is 300 lines, over the 200-line read window.*:\n900-900 f899 int f899\(\)\nRead the ones/,
    );
    assert.match(r.log, /\thit\tBash:tail\t\d+\t[^\t]*\t\tsrc\/a\.cpp\t601\t900\n$/);
  });
});

test("a budget another hook spends during a lookup is kept", async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  const counts = () => readSessionState("shunt-cap", "e2e", { tmp });
  // The stand-in server spends a symbol budget while it answers, as a parallel hook would.
  const spending = (key, answer) => ({
    "/v1/code.outline": () => {
      writeSessionState("shunt-cap", "e2e", { ...counts(), [key]: 1 }, { tmp });
      return answer;
    },
  });
  await withServer(spending("sym:one", { entries: [] }), (url) => runHook(readOf(dir, "src/a.cpp"), { tmp, url }));
  assert.deepEqual(counts(), { "sym:one": 1, "miss:src/a.cpp": 1 });
  const env = { LOOM_SHUNT_READ_CAP: "200" };
  const ranged = await withServer(spending("sym:two", OUTLINE["/v1/code.outline"]), (url) =>
    runHook(rangedRead(dir), { tmp, url, env }),
  );
  assert.ok(ranged.deny);
  assert.deepEqual(counts(), { "sym:one": 1, "miss:src/a.cpp": 1, "sym:two": 1, "src/a.cpp": 1 });
});

test("symlinks planted at the shunt's tmp files are not followed and cannot lock the agent out", {
  skip: !posix,
}, async () => {
  const { dir } = checkout({ "src/a.cpp": BIG });
  const tmp = seed(dir);
  const planted = {};
  const victims = {};
  await withServer(OUTLINE, async (url, seen) => {
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
  const tmp = seed(dir);
  const victim = path.join(tmp, "victim-log");
  fs.writeFileSync(victim, plantedBatch);
  await withServer(OUTLINE, async (url, seen) => {
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
  const tmp = seed(dir);
  fs.writeFileSync(cachePathFor(other, tmp), fs.readFileSync(cachePathFor(dir, tmp)));
  await withServer(OUTLINE, async (url, seen) => {
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
  const tmp = seed(dir);
  await withServer(OUTLINE, async (url, seen) => {
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
  const tmp = seed(dir);
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
  const tmp = seed(dir);
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
  const tmp = seed(dir);
  plantCap(tmp);
  const r = await withServer(OUTLINE, (url) =>
    runHook(rangedRead(dir), { tmp, url, env: { LOOM_SHUNT_READ_CAP: "200" } }),
  );
  assert.equal(r.deny, null);
  assert.match(r.log, /\tpass\tRead\t\d+\t\tbudget_unrecorded\t/);
});

for (const status of ["modified", "untracked"]) {
  for (const tool of ["Grep", "Bash"]) {
    test(`${status} ${tool} single-file grep stays local without indexed requests`, async (t) => {
      const { dir } = checkout({ "src/parser.py": "field_value = 1\n" });
      const rel = status === "untracked" ? "src/local_parser.py" : "src/parser.py";
      fs.writeFileSync(path.join(dir, rel), "field_value = 2\n");
      const tmp = seed(dir);
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
