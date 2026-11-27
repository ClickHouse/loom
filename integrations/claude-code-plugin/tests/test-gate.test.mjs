import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  checkoutPython,
  decideBlock,
  gatePayload,
  gateSignature,
  renderGate,
  sessionScope,
  venvPython,
} from "../lib/test-gate.mjs";

const GATE = {
  obligations: true,
  counts_floor: true,
  tests: [
    { test_path: "tests/unit/test_a.py", why: "stem_partner", run: "pytest tests/unit/test_a.py", run_unknown: false },
    { test_path: "tests/unit/test_a.py", why: "test_map", run: "pytest tests/unit/test_a.py", run_unknown: false },
    { test_path: "src/x/tests/gtest_parts.cpp", why: "graph_hop", run: "", run_unknown: true },
  ],
  untested: [
    { qualified_name: "DB::MergeTreeData::renameTempPartAndAdd", path: "src/a.cpp" },
    { qualified_name: "DB::MergeTreeData::renameTempPartAndAdd", path: "src/a.cpp" },
    { qualified_name: "DB::Foo::bar", path: "src/b.cpp" },
  ],
  untested_total: 2,
};

test("signature is order-independent over paths and bound to the diff", () => {
  assert.equal(gateSignature(["b.py", "a.py"], "h1"), gateSignature(["a.py", "b.py"], "h1"));
  assert.notEqual(gateSignature(["a.py"], "h1"), gateSignature(["a.py"], "h2"));
});

test("blocks once per signature, then lets the stop through", () => {
  const first = decideBlock({}, "sig");
  assert.equal(first.block, true);
  const second = decideBlock(first.cache, "sig");
  assert.equal(second.block, false);
  const other = decideBlock(second.cache, "sig2");
  assert.equal(other.block, true);
});

test("render dedupes commands, lists unknown-runner tests and untested names", () => {
  const text = renderGate(GATE);
  assert.match(text, /^Loom test gate \(graph is a floor\): run: pytest tests\/unit\/test_a\.py · /);
  assert.equal((text.match(/pytest tests\/unit\/test_a\.py/g) || []).length, 1);
  assert.match(text, /also reached \(no known runner\): src\/x\/tests\/gtest_parts\.cpp/);
  assert.match(text, /no test reaches: MergeTreeData::renameTempPartAndAdd, Foo::bar\./);
  assert.match(text, /Run the tests and rely on green/);
});

test("render caps and counts the overflow", () => {
  const many = {
    obligations: true,
    tests: Array.from({ length: 6 }, (_, i) => ({ run: `pytest t${i}.py` })),
    untested: [],
    untested_total: 0,
  };
  const text = renderGate(many, { maxRuns: 2 });
  assert.match(text, /pytest t0\.py ; pytest t1\.py \(\+4 more\)/);
});

test("a deleted indexed test is reported as missing, never prescribed as a runnable command", () => {
  const deleted = "tests/unit/test_migrations_schema_slice.py";
  const gate = {
    ...GATE,
    tests: [
      ...GATE.tests,
      { test_path: deleted, run: `pytest ${deleted}` },
      { test_path: deleted, run: `pytest ${deleted}` },
    ],
  };
  const text = renderGate(gate, {
    python: ".venv/bin/python",
    testExists: (p) => p !== deleted,
  });
  assert.match(text, /run: \.venv\/bin\/python -m pytest tests\/unit\/test_a\.py/);
  assert.doesNotMatch(text, /pytest tests\/unit\/test_migrations_schema_slice\.py/);
  assert.match(text, /indexed tests not present in this checkout: tests\/unit\/test_migrations_schema_slice\.py/);
  assert.equal((text.match(/test_migrations_schema_slice\.py/g) || []).length, 1);
  assert.match(text, /Missing tests are not passes/);
  assert.match(text, /no test reaches:/);
});

test("a gate with only unavailable tests remains an explicit obligation", () => {
  const text = renderGate(
    { obligations: true, tests: [{ test_path: "tests/old.py", run: "pytest tests/old.py" }] },
    { testExists: () => false },
  );
  assert.doesNotMatch(text, /run:|pytest/);
  assert.match(text, /indexed tests not present in this checkout: tests\/old\.py/);
  assert.match(text, /current equivalent tests/);
  assert.equal(gatePayload(text).decision, "block");
});

test("missing unknown-runner tests are distinguished from existing unknown-runner tests", () => {
  const text = renderGate(GATE, { testExists: (p) => p.endsWith(".py") });
  assert.doesNotMatch(text, /also reached \(no known runner\)/);
  assert.match(text, /indexed tests not present in this checkout: src\/x\/tests\/gtest_parts\.cpp/);
  assert.match(renderGate(GATE, { testExists: () => true }), /also reached \(no known runner\)/);
});

test("missing-path reporting is bounded and does not mutate the server response", () => {
  const gate = {
    obligations: true,
    tests: Array.from({ length: 6 }, (_, i) => ({ test_path: `tests/t${i}.py`, run: `pytest tests/t${i}.py` })),
  };
  const original = structuredClone(gate);
  const text = renderGate(gate, { maxNames: 2, testExists: () => false });
  assert.match(text, /tests\/t0\.py, tests\/t1\.py \(\+4\)/);
  assert.deepEqual(gate, original);
});

test("no obligations renders nothing and produces no payload", () => {
  assert.equal(renderGate({ obligations: false, tests: [], untested: [] }), "");
  assert.equal(renderGate(null), "");
  assert.equal(gatePayload(""), null);
  assert.deepEqual(gatePayload("x"), { decision: "block", reason: "x" });
});

// --- sessionScope: the change is the session's, not the tree's

const ROOT = "/r";
const use = (name, input, extra = {}) => ({
  type: "assistant",
  message: { content: [{ type: "tool_use", name, input }] },
  ...extra,
});
const DIRTY = ["src/a.py", "src/b.py", "tests/test_a.py", "a.py", "docs/notes.md"];

test("a dirty tree the session never touched gates nothing", () => {
  assert.deepEqual(sessionScope(DIRTY, [], { root: ROOT }), []);
  assert.deepEqual(
    sessionScope(DIRTY, [{ type: "user", message: { content: "edit src/a.py please" } }], { root: ROOT }),
    [],
  );
  assert.deepEqual(sessionScope([], [use("Edit", { file_path: "/r/src/a.py" })], { root: ROOT }), []);
});

test("edit-tool targets count, absolute or relative to cwd, in dirty order", () => {
  const objs = [
    use("Write", { file_path: "/r/tests/test_a.py" }),
    use("Edit", { file_path: "a.py" }), // relative to cwd /r/src → src/a.py
    use("MultiEdit", { edits: [{ file_path: "/r/docs/notes.md" }] }),
    use("Edit", { file_path: "/elsewhere/a.py" }), // outside the repo
  ];
  assert.deepEqual(sessionScope(DIRTY, objs, { root: ROOT, cwd: "/r/src" }), [
    "src/a.py",
    "tests/test_a.py",
    "docs/notes.md",
  ]);
});

test("a shell command naming the path counts; a longer name containing it does not", () => {
  const sed = [use("Bash", { command: "sed -i 's/x/y/' src/b.py && git diff --stat" })];
  assert.deepEqual(sessionScope(DIRTY, sed, { root: ROOT }), ["src/b.py"]);
  const abs = [use("Bash", { command: "cat > /r/docs/notes.md <<'EOF'\nhi\nEOF" })];
  assert.deepEqual(sessionScope(DIRTY, abs, { root: ROOT }), ["docs/notes.md"]);
  const pytest = [use("Bash", { command: "pytest tests/test_a.py::test_x -q" })];
  assert.deepEqual(sessionScope(DIRTY, pytest, { root: ROOT }), ["tests/test_a.py"]);
  const near = [use("Bash", { command: "python data.py; ls src/a.pyc; echo /other/r/src/b.py" })];
  assert.deepEqual(sessionScope(DIRTY, near, { root: ROOT }), []);
});

test("on Windows a command names an absolute path with either slash or drive case, or as Git Bash writes it", {
  skip: process.platform !== "win32",
}, () => {
  const root = "C:\\r";
  for (const named of ["C:\\r\\src\\b.py", "C:/r/src/b.py", "c:/r/src/b.py", "c:\\r\\src\\b.py", "/c/r/src/b.py"]) {
    const cmd = [use("Bash", { command: `sed -i 's/x/y/' ${named}` })];
    assert.deepEqual(sessionScope(["src/b.py"], cmd, { root }), ["src/b.py"], named);
  }
});

test("a virtualenv's interpreter: bin/python, and Scripts\\python.exe on Windows", () => {
  assert.equal(venvPython("/r/.venv", "linux"), "/r/.venv/bin/python");
  assert.equal(venvPython("/r/.venv", "darwin"), "/r/.venv/bin/python");
  assert.equal(venvPython("C:\\r\\.venv", "win32"), "C:\\r\\.venv\\Scripts\\python.exe");
});

test("a PowerShell command naming the path counts, as a Bash one does", () => {
  const ps = [use("PowerShell", { command: "(Get-Content src/b.py) -replace 'x','y' | Set-Content src/b.py" })];
  assert.deepEqual(sessionScope(DIRTY, ps, { root: ROOT }), ["src/b.py"]);
});

test("Codex shapes: argv shell commands and apply_patch envelopes", () => {
  const shell = [use("shell", { command: ["bash", "-lc", "sed -i 's/x/y/' src/a.py"] })];
  assert.deepEqual(sessionScope(DIRTY, shell, { root: ROOT }), ["src/a.py"]);
  const patch = [
    use("apply_patch", { input: "*** Begin Patch\n*** Update File: tests/test_a.py\n@@\n-a\n+b\n*** End Patch" }),
  ];
  assert.deepEqual(sessionScope(DIRTY, patch, { root: ROOT, cwd: ROOT }), ["tests/test_a.py"]);
});

test("Codex rollout tool calls scope the gate to paths named by this session", () => {
  const rows = [
    {
      type: "response_item",
      payload: {
        type: "custom_tool_call",
        name: "exec",
        input: "const r=await tools.exec_command({cmd:\"python3 -c 'edit src/a.py'\"});text(r)",
      },
    },
    {
      type: "response_item",
      payload: {
        type: "custom_tool_call",
        name: "exec",
        input: 'await tools.apply_patch("*** Begin Patch\\n*** Update File: tests/test_a.py\\n*** End Patch")',
      },
    },
    { type: "response_item", payload: { type: "message", content: "src/b.py" } },
  ];
  assert.deepEqual(sessionScope(DIRTY, rows, { root: ROOT }), ["src/a.py", "tests/test_a.py"]);
});

test("sidechain edits and non-assistant rows are not the session's change", () => {
  const objs = [
    use("Edit", { file_path: "/r/src/a.py" }, { isSidechain: true }),
    { type: "user", message: { content: [{ type: "tool_result", content: "M src/b.py" }] } },
  ];
  assert.deepEqual(sessionScope(DIRTY, objs, { root: ROOT }), []);
});

// ---- ---------------------------------------------------------

import { commentOnlyDiff, withPython } from "../lib/test-gate.mjs";

const hunk = (file, lines) =>
  `diff --git a/${file} b/${file}\n--- a/${file}\n+++ b/${file}\n@@ -1,0 +2 @@\n${lines.join("\n")}\n`;

test("a comment- or blank-only diff owes no test", () => {
  assert.equal(commentOnlyDiff(hunk("server/naming.py", ["+# loomcheck"])), true);
  assert.equal(commentOnlyDiff(hunk("lib/a.mjs", ["+// note", "+", "-/* old */"])), true);
  assert.equal(commentOnlyDiff(hunk("q.sql", ["+-- why"])), true);
  assert.equal(commentOnlyDiff(hunk("server/naming.py", ["+# note", "+x = 1"])), false);
  // `*ptr = 1;` is C, not a comment line
  assert.equal(commentOnlyDiff(hunk("src/a.cpp", ["+*ptr = 1;"])), false);
  // an unknown language, a rename and an empty diff count as code
  assert.equal(commentOnlyDiff(hunk("data.csv", ["+# a,b"])), false);
  assert.equal(commentOnlyDiff("diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\n"), false);
  assert.equal(commentOnlyDiff(""), false);
  // every file of a multi-file diff must be comment-only
  assert.equal(commentOnlyDiff(hunk("a.py", ["+# x"]) + hunk("b.py", ["+y()"])), false);
});

test("pytest runs use the checkout's interpreter; none found is said plainly", () => {
  assert.equal(withPython("pytest tests/a.py", ".venv/bin/python"), ".venv/bin/python -m pytest tests/a.py");
  assert.equal(withPython("go test ./x/", ".venv/bin/python"), "go test ./x/");
  assert.equal(withPython("pytest tests/a.py", undefined), "pytest tests/a.py");
  const found = renderGate(GATE, { python: ".venv/bin/python" });
  assert.match(found, /run: \.venv\/bin\/python -m pytest tests\/unit\/test_a\.py/);
  assert.doesNotMatch(found, /No Python was found/);
  const none = renderGate(GATE, { python: "" });
  assert.match(none, /No Python was found for this checkout/);
  assert.match(renderGate(GATE), /run: pytest tests\/unit\/test_a\.py/);
});

test("nothing to run asks for a test, not for running tests", () => {
  const text = renderGate({
    obligations: true,
    untested: [{ qualified_name: "server.naming::validate_namespace_name" }],
  });
  assert.match(text, /no test reaches: server\.naming::validate_namespace_name/);
  assert.match(text, /Add a test that reaches it, or say why the change needs none/);
  assert.doesNotMatch(text, /Run the tests/);
});

test("checkoutPython names a PATH python without running it", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-path-"));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-"));
  const marker = path.join(dir, "ran");
  const win = process.platform === "win32";
  const write = (name, mode = 0o755) =>
    fs.writeFileSync(path.join(dir, win ? `${name}.exe` : name), `#!/bin/sh\ntouch ${JSON.stringify(marker)}\n`, {
      mode,
    });
  const env = { PATH: `${path.delimiter}${win ? `"${dir}"` : dir}` };
  try {
    assert.equal(checkoutPython(root, { env }), "");
    write("python");
    assert.equal(checkoutPython(root, { env }), "python");
    write("python3");
    // Windows looks for python first: python.org installs ship no python3.exe.
    assert.equal(checkoutPython(root, { env }), win ? "python" : "python3");
    assert.equal(fs.existsSync(marker), false);
    if (!win) {
      fs.chmodSync(path.join(dir, "python3"), 0o644); // a shell would not run it
      assert.equal(checkoutPython(root, { env }), "python");
    }
    // An empty file, like the Store aliases Windows puts on PATH, does not count.
    for (const f of fs.readdirSync(dir)) fs.writeFileSync(path.join(dir, f), "");
    assert.equal(checkoutPython(root, { env }), "");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("checkoutPython prefers the checkout's virtualenvs, then an active one, then PATH", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-"));
  const active = fs.mkdtempSync(path.join(os.tmpdir(), "loom-venv-"));
  const make = (venv) => {
    const py = venvPython(venv);
    fs.mkdirSync(path.dirname(py), { recursive: true });
    fs.writeFileSync(py, "");
    return py;
  };
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), "loom-path-"));
  const pathPy = process.platform === "win32" ? ["python", ".exe"] : ["python3", ""];
  fs.writeFileSync(path.join(bin, pathPy.join("")), "#!/bin/sh\n", { mode: 0o755 });
  const env = { PATH: bin, VIRTUAL_ENV: active };
  try {
    assert.equal(checkoutPython(root, { env }), pathPy[0]);
    const activePy = make(active);
    assert.equal(checkoutPython(root, { env }), activePy);
    make(path.join(root, "venv"));
    assert.equal(
      checkoutPython(root, { env }),
      path
        .relative(root, venvPython(path.join(root, "venv")))
        .split(path.sep)
        .join("/"),
    );
    make(path.join(root, ".venv"));
    assert.equal(
      checkoutPython(root, { env }),
      path
        .relative(root, venvPython(path.join(root, ".venv")))
        .split(path.sep)
        .join("/"),
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(bin, { recursive: true, force: true });
    fs.rmSync(active, { recursive: true, force: true });
  }
});

test("the test gate's lib and hook do not import node:child_process", () => {
  for (const file of ["../lib/test-gate.mjs", "../hooks/loom-test-gate.mjs"]) {
    const src = fs.readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(src, /child_process/, file);
  }
});

test("checkoutPython runs no pytest.py planted in the checkout", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-"));
  const marker = path.join(root, "ran");
  fs.writeFileSync(path.join(root, "pytest.py"), `open(${JSON.stringify(marker)}, "w").write("x")\n`);
  const cwd = process.cwd();
  process.chdir(root); // a Stop hook runs with the checkout as working directory
  try {
    checkoutPython(root, { env: { PATH: process.env.PATH } });
    assert.equal(fs.existsSync(marker), false);
  } finally {
    process.chdir(cwd);
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// --- runAnswered: a transcript that already answers the ask is not blocked

import http from "node:http";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { declinesTests, newestChange, runAnswered } from "../lib/test-gate.mjs";
import { cachePathFor } from "../lib/repo-scope.mjs";
import { hostEnv } from "./host-env.mjs";

const said = (content, extra = {}) => ({ type: "user", message: { content }, ...extra });
let calls = 0;
/** A tool call and its result row, as Claude Code writes them, a second apart. */
const call = (name, input, output = "", extra = {}) => {
  const id = `toolu_${++calls}`;
  const at = (ms) => new Date(1e12 + calls * 2000 + ms).toISOString();
  return [
    { type: "assistant", timestamp: at(0), message: { content: [{ type: "tool_use", id, name, input }] }, ...extra },
    {
      type: "user",
      timestamp: at(1000),
      message: { content: [{ type: "tool_result", tool_use_id: id, content: output }] },
      ...extra,
    },
  ];
};
const bash = (command, output, extra) => call("Bash", { command }, output, extra);
// A call that left a file of the tree newer than every row before it.
const WROTE = { wroteTree: true };
const edit = () => call("Edit", { file_path: "/r/src/a.py" }, "updated", WROTE);
const PASSED = "........ [100%]\n467 passed, 1 skipped, 1 xfailed, 2 xpassed in 1.11s\nexit=0";
const FAILED = "63 failed, 400 passed, 1 skipped, 1 xfailed, 2 xpassed, 5 errors in 1.60s\nexit=1";
// As sessions write it: the edit and the run in one command, the exit status behind a pipe.
const SUITE = "python -m pytest tests -q --tb=short -p no:cacheprovider 2>&1 | tail -5; echo exit=${PIPESTATUS[0]}";
const HEREDOC =
  "cat >> tests/test_a.py <<'EOF'\nimport pytest\n\ndef test_new():\n    with pytest.raises(ValueError):\n        f()\nEOF\n";
const RUN_GATE = {
  obligations: true,
  tests: [{ test_path: "tests/test_a.py", run: "pytest tests/test_a.py" }],
  untested: [{ qualified_name: "m::f" }],
};
const answered = (objs, gate = RUN_GATE, cwd = ROOT) =>
  runAnswered(gate, objs.flat(), {
    root: ROOT,
    cwd,
    testExists: (p) => !p.includes("gone"),
    lastModified: () => Math.max(0, ...objs.flat().map((o) => (o.wroteTree ? Date.parse(o.timestamp) : 0))),
  });

test("a clean pytest run of the named test after the last change answers the gate", () => {
  assert.equal(answered([edit(), bash(HEREDOC + SUITE, PASSED)]), true);
  assert.equal(answered([edit(), bash("pytest tests/test_a.py", "===== 3 passed in 0.12s =====")]), true);
  assert.equal(
    answered([edit(), bash(".venv/bin/pytest /r/tests/test_a.py -q", "3 passed, 1 warning in 62.10s (0:01:02)")]),
    true,
  );
  // A tool that neither edits nor runs a shell forgets nothing.
  assert.equal(answered([edit(), bash(SUITE, PASSED), call("Read", { file_path: "/r/src/a.py" }, "x")]), true);
  // Two files, two runs, no change between them.
  const two = {
    ...RUN_GATE,
    tests: [...RUN_GATE.tests, { test_path: "tests/test_b.py", run: "pytest tests/test_b.py" }],
  };
  const runs = [
    edit(),
    bash("pytest tests/test_a.py -q", "3 passed in 0.10s"),
    bash("pytest tests/test_b.py -q", "2 passed in 0.10s"),
  ];
  assert.equal(answered(runs, two), true);
  assert.equal(answered(runs.slice(0, 2), two), false);
  for (const launched of [
    "timeout 300 .venv/bin/python -m pytest tests -q",
    "env -u CI LOOM_ENV_FILE= $PY -m pytest tests -q",
    "uv run pytest tests -q",
  ]) {
    assert.equal(answered([edit(), bash(launched, "3 passed in 0.10s")]), true, launched);
  }
});

test("a run's paths are read from the directory its command moved to", () => {
  const nested = { ...RUN_GATE, tests: [{ test_path: "sub/tests/test_a.py", run: "pytest sub/tests/test_a.py" }] };
  assert.equal(answered([edit(), bash("cd /r; python -m pytest tests -q", "3 passed in 0.10s")]), true);
  assert.equal(answered([edit(), bash("cd sub && pytest tests -q", "3 passed in 0.10s")], nested), true);
  assert.equal(answered([edit(), bash("cd sub && pytest tests -q", "3 passed in 0.10s")]), false);
  assert.equal(answered([edit(), bash("(cd sub && pytest tests -q)", "3 passed in 0.10s")]), false);
  assert.equal(
    answered([edit(), bash("cd sub && echo $(date) && pytest tests -q", "3 passed in 0.10s")], nested),
    true,
  );
  assert.equal(answered([edit(), bash("cd /tmp && pytest tests -q", "3 passed in 0.10s")]), false);
  assert.equal(
    answered([edit(), bash("pytest sub -q; cd sub && pytest tests -q", "1 passed in 0.10s\n3 passed in 0.10s")]),
    false,
  );
  // The row's own directory, not the one the session stops in.
  const inSub = [edit(), bash("pytest tests -q", "3 passed in 0.10s", { cwd: "/r/sub" })];
  assert.equal(answered(inSub, nested), true);
  assert.equal(answered(inSub), false);
  assert.equal(
    answered([edit(), bash("pytest tests -q", "3 passed in 0.10s", { cwd: ROOT })], RUN_GATE, "/r/sub"),
    true,
  );
  // A move this reader does not follow, and a run outside the checkout.
  for (const elsewhere of [
    "pushd sub && pytest tests -q",
    "(cd sub && make build); pytest tests -q",
    "cd a && cd sub && pytest tests -q",
    "docker exec app pytest tests -q",
    "kubectl exec pod -- pytest tests -q",
    "ssh host 'cd repo; pytest tests -q'",
    "ssh host bash <<'EOF'\npytest tests -q\nEOF",
  ]) {
    assert.equal(answered([edit(), bash(elsewhere, "3 passed in 0.10s")]), false, elsewhere);
    assert.equal(answered([edit(), bash(elsewhere, "3 passed in 0.10s")], nested), false, elsewhere);
  }
  // A PowerShell run is not read: its moves are not ones the reader knows.
  for (const command of ["pytest tests -q", "Set-Location sub; pytest tests -q"]) {
    const rows = [edit(), call("PowerShell", { command }, "3 passed in 0.10s")];
    assert.equal(answered(rows), false, command);
    assert.equal(answered(rows, nested), false, command);
  }
});

test("a run before the tree's last change, an edit or another shell command answers nothing", () => {
  assert.equal(answered([edit()]), false);
  assert.equal(answered([bash(SUITE, PASSED), edit()]), false);
  // An edit that puts a file back as HEAD has it, or writes an ignored one, leaves no newer file.
  for (const [tool, file] of [
    ["Edit", "/r/src/b.py"],
    ["Write", "/r/.cache/fixture.json"],
  ]) {
    assert.equal(answered([edit(), bash(SUITE, PASSED), call(tool, { file_path: file }, "updated")]), false, tool);
  }
  // A change the transcript does not show as one, such as a subagent's edit: the file is newer.
  assert.equal(
    answered([edit(), bash(SUITE, PASSED), call("Agent", { prompt: "tidy src/a.py" }, "done", WROTE)]),
    false,
  );
  assert.equal(answered([edit(), bash(SUITE, PASSED), call("Agent", { prompt: "explain src/a.py" }, "done")]), true);
  // Any other shell command after the run: a delete or a copy can leave no newer file, and a read is not told apart.
  for (const later of [
    "ruff check --fix .",
    "rm src/helper.py",
    "mv src/helper.py src/util.py",
    "cp -p /tmp/a.py src/b.py",
    "git status --short",
    "git diff -- src/a.py",
  ]) {
    assert.equal(answered([edit(), bash(SUITE, PASSED), bash(later, "")]), false, later);
  }
  assert.equal(answered([edit(), bash(SUITE, PASSED), call("PowerShell", { command: "Get-Date" }, "")]), false);
  // A result row without a time cannot be placed after the change.
  const [ran, result] = bash(SUITE, PASSED);
  assert.equal(answered([edit(), ran, { ...result, timestamp: undefined }]), false);
  // The run's own command does more than run pytest and show output: its result row is later than what it wrote.
  for (const more of [
    "pytest tests -q; sed -i s/a/b/ src/a.py",
    "pytest tests -q; ruff check --fix .",
    "pytest tests -q && rm src/helper.py",
    "pytest tests -q\npython scripts/gen.py",
    "pytest tests -q | tee out.log",
    "pytest tests -q > out.log",
    "pytest tests -q; echo x > src/new.py",
    "pytest tests -q; echo $(rm src/helper.py)",
    "pytest tests -q; echo `rm src/helper.py`",
    "pytest `rm src/helper.py` tests -q",
  ]) {
    assert.equal(answered([edit(), bash(more, "3 passed in 0.10s")]), false, more);
  }
  // Output handling is all that may follow.
  for (const shown of [
    "pytest tests -q 2>&1 | tail -20",
    "pytest tests -q | head -50 && echo OK || echo FAIL",
    "pytest tests -q 2>&1 | grep passed; echo exit=$?",
    "(cd /r && pytest tests -q)",
  ]) {
    assert.equal(answered([edit(), bash(shown, "3 passed in 0.10s")]), true, shown);
  }
  // The suite ran on the tree without the change.
  assert.equal(
    answered([edit(), bash("git stash -q; python -m pytest tests -q | tail -1; git stash pop -q", PASSED)]),
    false,
  );
  assert.equal(answered([edit(), bash(SUITE, PASSED), bash("git checkout -- src/a.py", "")]), false);
  assert.equal(answered([edit(), bash(SUITE, PASSED, { isSidechain: true })]), false);
});

test("a run that failed, ran a subset or ran another file answers nothing", () => {
  assert.equal(answered([edit(), bash(SUITE, FAILED)]), false);
  assert.equal(answered([edit(), bash(SUITE, PASSED), bash(SUITE, FAILED)]), false);
  assert.equal(answered([edit(), bash("pytest tests -q -k new", "1 passed, 4 deselected in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests/test_a.py::test_new -q", "1 passed in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests --ignore=tests/test_a.py -q", "9 passed in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests/test_b.py -q", "2 passed in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests 2>&1 | grep -B5 NameError | head", "")]), false);
  assert.equal(
    answered([edit(), bash("echo ran python -m pytest tests/test_a.py", "ran python -m pytest tests/test_a.py")]),
    false,
  );
  // A flag's value is not a path that ran.
  const colocated = { ...RUN_GATE, tests: [{ test_path: "src/pkg/test_mod.py", run: "pytest src/pkg/test_mod.py" }] };
  for (const flagged of ["pytest tests -q --cov src", "pytest --rootdir src tests -q", "pytest -c src/pkg tests"]) {
    assert.equal(answered([edit(), bash(flagged, "3 passed in 0.10s")], colocated), false, flagged);
    assert.equal(answered([edit(), bash(flagged, "3 passed in 0.10s")]), true, flagged);
  }
  assert.equal(answered([edit(), bash("pytest -xq --cov=tests src", "3 passed in 0.10s")], colocated), true);
  // -qq prints no summary: one clean line cannot speak for two runs.
  assert.equal(
    answered([edit(), bash("pytest tests/test_a.py -qq; pytest tests/test_b.py -q", "2 passed in 0.10s")]),
    false,
  );
});

test("a gate that asks for more than a pytest run of present tests still asks", () => {
  const no = [said("Fix f. Do not run the test suite."), edit()];
  const ran = [edit(), bash(SUITE, PASSED)];
  const missing = {
    ...RUN_GATE,
    tests: [...RUN_GATE.tests, { test_path: "tests/gone_test.py", run: "pytest tests/gone_test.py" }],
  };
  const other = { ...RUN_GATE, tests: [{ test_path: "tests/a.test.ts", run: "npx vitest run tests/a.test.ts" }] };
  const untested = { ...RUN_GATE, tests: [] };
  assert.equal(answered(ran, missing), false);
  assert.equal(answered(no, missing), false);
  assert.equal(answered(ran, other), false);
  assert.equal(answered(ran, untested), false);
  assert.equal(answered(no, untested), false);
  assert.equal(answered(no), true);
  assert.equal(answered(no, other), true);
});

test("only the last sentence of the person's latest prompt can rule the tests out", () => {
  for (const yes of [
    "Fix f. Do not run the test suite.",
    "[run 12] Fix f in this run. Do not run the test suite.",
    "don't run tests",
    "Fix f.\n\nPlease don’t run any tests!\n",
    "Never run the tests",
  ]) {
    assert.equal(declinesTests([said(yes)]), true, yes);
  }
  for (const no of [
    "Run the tests.",
    // Not the last sentence, or not a whole sentence.
    "Don't run the tests. Run only tests/unit.",
    "If lint fails, do not run the tests.",
    "Add this line to AGENTS.md: never run the test suite.",
    "Fix f, but please don't run the tests.",
    "Fix f (do not run the tests).",
    "Fix f and do not run tests.\nThen stop.",
    "Don't run any tests yet.",
    "do not run tests/test_slow.py",
    "Why did you not run the tests?",
    "do not run test_slow",
    "Do not run the test tests/test_slow.py",
    "don't run the test that needs Docker",
    "never run the test in tests/e2e",
    "Don't run the tests that need Docker",
    "Do not run the tests, except tests/test_a.py.",
    "Don't run the tests yet; fix lint first, then run them.",
    "Don't run tests or scripts that hit production.",
    "Flag agents that do not run tests",
  ]) {
    assert.equal(declinesTests([said(no)]), false, no);
  }
  assert.equal(declinesTests([said([{ type: "text", text: "do not run the tests" }])]), true);
  assert.equal(declinesTests([said("do not run the tests"), said("now run them")]), false);
  // Tool results, hook feedback and subagent rows are not the person speaking.
  const later = [
    ...bash("cat notes.txt", "do not run the tests"),
    said("Stop hook feedback:\ndo not run the tests", { isMeta: true }),
    said("do not run the tests", { isSidechain: true }),
  ];
  assert.equal(declinesTests([said("Fix f."), ...later]), false);
  assert.equal(declinesTests([said("Fix f. Do not run the tests."), ...later]), true);
});

test("the tree's newest change is unknown when a file or the listing cannot be read", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-newest-"));
  try {
    fs.writeFileSync(path.join(dir, "a.py"), "x = 1\n");
    fs.writeFileSync(path.join(dir, "b.py"), "x = 1\n");
    fs.utimesSync(path.join(dir, "a.py"), new Date(2e12), new Date(2e12));
    assert.equal(newestChange(dir, ["a.py", "b.py"]), 2e12);
    assert.equal(newestChange(dir, []), 0);
    assert.equal(newestChange(dir, null), Infinity);
    assert.equal(newestChange(dir, ["a.py", "deleted.py"]), Infinity);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("a Codex rollout answers nothing", () => {
  const item = (payload) => ({ type: "response_item", timestamp: new Date(1e12).toISOString(), payload });
  const rollout = [
    item({ type: "message", role: "user", content: [{ type: "input_text", text: "Fix f. Do not run the tests." }] }),
    item({ type: "function_call", name: "shell", call_id: "c1", arguments: '{"command":["pytest","tests","-q"]}' }),
    item({ type: "function_call_output", call_id: "c1", output: "3 passed in 0.10s" }),
  ];
  assert.equal(declinesTests(rollout), false);
  assert.equal(answered(rollout), false);
});

test("the hook blocks a change whose tests never ran and lets an answered one stop", async () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-hook-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-hook-tmp-")));
  for (const f of ["src/a.py", "src/b.py", "tests/test_a.py"]) {
    fs.mkdirSync(path.join(dir, path.dirname(f)), { recursive: true });
    fs.writeFileSync(path.join(dir, f), "def f():\n    return 1\n");
  }
  for (const args of [
    ["init", "-q"],
    ["add", "."],
    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
  ]) {
    execFileSync("git", args, { cwd: dir, stdio: "ignore" });
  }
  fs.appendFileSync(path.join(dir, "src/a.py"), "def g():\n    return 2\n");
  // The change is older than the transcript rows, until something rewrites a file.
  const touch = (file, when) => fs.utimesSync(path.join(dir, file), when, when);
  touch("src/a.py", new Date(1e12 - 1000));
  fs.writeFileSync(
    cachePathFor(dir, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
  const server = http.createServer((req, res) => {
    req.resume();
    req.on("end", () => res.end(JSON.stringify(req.url === "/v1/code.test_gate" ? RUN_GATE : {})));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const hook = fileURLToPath(new URL("../hooks/loom-test-gate.mjs", import.meta.url));
  const stop = (session, rows) =>
    new Promise((resolve) => {
      const transcript = path.join(tmp, `${session}.jsonl`);
      fs.writeFileSync(
        transcript,
        rows
          .flat()
          .map((o) => JSON.stringify(o))
          .join("\n") + "\n",
      );
      const child = spawn(process.execPath, [hook, "Stop"], {
        env: hostEnv({
          PATH: process.env.PATH,
          HOME: tmp,
          TMPDIR: tmp,
          LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
          LOOM_API_KEY: "t",
        }),
        stdio: ["pipe", "pipe", "ignore"],
      });
      let out = "";
      child.stdout.on("data", (c) => (out += c));
      child.on("close", () => resolve(out));
      child.stdin.end(JSON.stringify({ cwd: dir, session_id: session, transcript_path: transcript }));
    });
  const change = call("Edit", { file_path: path.join(dir, "src/a.py") }, "updated");
  try {
    const ran = [change, bash(SUITE, PASSED)];
    const no = [said("Fix f. Do not run the test suite."), change];
    assert.match(await stop("never-ran", [change]), /"decision":"block".*pytest tests\/test_a\.py/);
    assert.equal(await stop("never-ran", [change]), "");
    // An untracked file whose name git would quote or a trim would cut does not hide the tree's age.
    const odd = process.platform === "win32" ? " naïve.txt" : ' na"ï\tve.txt';
    fs.writeFileSync(path.join(dir, odd), "x\n");
    touch(odd, new Date(1e12 - 1000));
    assert.equal(await stop("ran", ran), "");
    // A shell command after the run may have deleted a file, which leaves nothing newer.
    assert.match(await stop("removed-after-run", [...ran, bash("rm src/helper.py", "")]), /"decision":"block"/);
    // A silent stop leaves the change its one block.
    assert.equal(await stop("ruled-out", no), "");
    assert.match(await stop("ruled-out", [...no, said("now run them")]), /"decision":"block"/);
    // A file changed after the run, with no transcript row for it: new, deleted, rewritten.
    fs.writeFileSync(path.join(dir, "tests/conftest.py"), "x = 1\n");
    assert.match(await stop("ran", ran), /"decision":"block"/);
    fs.rmSync(path.join(dir, "tests/conftest.py"));
    fs.rmSync(path.join(dir, "src/b.py"));
    assert.match(await stop("deleted-after-run", ran), /"decision":"block"/);
    execFileSync("git", ["checkout", "-q", "--", "src/b.py"], { cwd: dir });
    touch("src/b.py", new Date(1e12 - 1000));
    touch("src/a.py", new Date());
    assert.match(await stop("edited-after-run", ran), /"decision":"block"/);
  } finally {
    server.closeAllConnections();
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
