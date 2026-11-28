import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  advicePayload,
  adviceTests,
  checkoutPython,
  decideBlock,
  gateMode,
  gatePayload,
  gateSignature,
  printedTests,
  renderAdvice,
  renderGate,
  sessionScope,
  sessionStart,
  venvPython,
  writtenSince,
} from "../lib/test-gate.mjs";

const GATE = {
  obligations: true,
  counts_floor: true,
  tests: [
    { test_path: "tests/unit/test_a.py", why: "changed_test", run: "pytest tests/unit/test_a.py", run_unknown: false },
    { test_path: "tests/unit/test_a.py", why: "test_map", run: "pytest tests/unit/test_a.py", run_unknown: false },
    { test_path: "src/x/tests/gtest_parts.cpp", why: "static_call", run: "", run_unknown: true },
  ],
  untested: [],
  unlinked: [
    { qualified_name: "DB::MergeTreeData::renameTempPartAndAdd", path: "src/a.cpp" },
    { qualified_name: "DB::MergeTreeData::renameTempPartAndAdd", path: "src/a.cpp" },
    { qualified_name: "DB::Foo::bar", path: "src/b.cpp" },
  ],
  unlinked_total: 2,
};

test("the gate advises at edit time unless LOOM_TEST_GATE says block or off", () => {
  assert.equal(gateMode({}), "advise");
  for (const v of ["on", "1", "true", "", "advise", "anything"])
    assert.equal(gateMode({ LOOM_TEST_GATE: v }), "advise", v);
  for (const v of ["block", " Block ", "BLOCK"]) assert.equal(gateMode({ LOOM_TEST_GATE: v }), "block", v);
  for (const v of ["off", "0", "false", "no"]) assert.equal(gateMode({ LOOM_TEST_GATE: v }), "off", v);
});

test("the edit-time line names the commands of the tests the index links to the file", () => {
  const text = renderAdvice("src/a.py", adviceTests("src/a.py", GATE), { python: ".venv/bin/python" });
  assert.equal(
    text,
    "Loom tests for src/a.py: .venv/bin/python -m pytest tests/unit/test_a.py · no known runner: src/x/tests/gtest_parts.cpp",
  );
  // Beside tests to run no function is named: the graph is a floor, and the line asks for no reply.
  assert.doesNotMatch(text, /renameTempPartAndAdd|no test reaches|links no test/);
  assert.equal(
    renderAdvice("src/a.py", adviceTests("src/a.py", GATE), { python: ".venv/bin/python", unlinked: "Foo::bar" }),
    text,
  );
  assert.deepEqual(advicePayload(text), {
    hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: text },
  });
});

test("a changed test names its own command; a file no runner takes, a test the checkout lacks and an empty answer name nothing", () => {
  const line = (rel, gate, testExists) => renderAdvice(rel, adviceTests(rel, gate, testExists));
  const own = { tests: [{ test_path: "tests/test_a.py", why: "changed_test", run: "pytest tests/test_a.py" }] };
  assert.equal(line("tests/test_a.py", own), "Loom tests for tests/test_a.py: pytest tests/test_a.py");
  // A fixture or a conftest under tests/ is the gate's row for itself, with no runner.
  const fixture = { tests: [{ test_path: "tests/conftest.py", why: "changed_test", run: "", run_unknown: true }] };
  assert.equal(line("tests/conftest.py", fixture), "");
  const gone = { tests: [{ test_path: "tests/gone.py", why: "test_map", run: "pytest tests/gone.py" }] };
  assert.equal(
    line("src/a.py", gone, () => false),
    "",
  );
  assert.equal(
    line("src/a.py", gone, () => true),
    "Loom tests for src/a.py: pytest tests/gone.py",
  );
  for (const none of [null, {}, { tests: [] }, { unlinked: [{ qualified_name: "m::f" }] }]) {
    assert.equal(line("src/a.py", none), "");
  }
  assert.equal(advicePayload(""), null);
});

test("a file with no test to name gets the functions the index links no test to, as the index's view", () => {
  assert.equal(
    renderAdvice("src/a.py", [], { unlinked: "MergeTreeData::renameTempPartAndAdd, Foo::bar" }),
    "Loom tests for src/a.py: the index links no test to MergeTreeData::renameTempPartAndAdd, Foo::bar",
  );
  assert.equal(renderAdvice("src/a.py", [], { unlinked: "" }), "");
});

test("the edit-time line folds pytest files into one command and counts the commands it leaves out", () => {
  const many = ["a", "b", "c", "d", "e", "f"].map((n) => ({
    test_path: `tests/test_${n}.py`,
    why: "static_call",
    run: `pytest tests/test_${n}.py`,
  }));
  assert.equal(
    renderAdvice("src/a.py", many),
    "Loom tests for src/a.py: pytest tests/test_a.py tests/test_b.py tests/test_c.py tests/test_d.py (+2 more)",
  );
  // The two it leaves out were not shown, so they are not marked as named.
  const unknown = ["g", "h", "i", "j", "k"].map((n) => ({ test_path: `spec/${n}.cpp`, run: "" }));
  assert.deepEqual(
    printedTests([...many, ...unknown]).map((t) => t.test_path),
    [...many.slice(0, 4), ...unknown.slice(0, 4)].map((t) => t.test_path),
  );
});

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

test("render dedupes commands, lists unknown-runner tests and unlinked names", () => {
  const text = renderGate(GATE);
  assert.match(text, /^Loom test gate \(graph is a floor\): run: pytest tests\/unit\/test_a\.py · /);
  assert.equal((text.match(/pytest tests\/unit\/test_a\.py/g) || []).length, 1);
  assert.match(text, /also reached \(no known runner\): src\/x\/tests\/gtest_parts\.cpp · /);
  assert.match(text, /links no test to: MergeTreeData::renameTempPartAndAdd, Foo::bar \(the tests named here may/);
  assert.match(text, /\. Run the tests before finishing, or say why not\.$/);
});

test("an unlinked definition is the index's view, never a fact about the tests", () => {
  // A constructor, and a test file whose every test builds the object: the graph holds no edge between them.
  const built = {
    obligations: true,
    counts_floor: true,
    tests: [{ test_path: "tests/test_widgets.py", why: "changed_test", run: "pytest tests/test_widgets.py" }],
    unlinked: [{ qualified_name: "src.pkg.widgets::Widget::__init__", path: "src/pkg/widgets.py" }],
    unlinked_total: 1,
  };
  assert.equal(
    renderGate(built, { python: "python3", testExists: () => true }),
    "Loom test gate (graph is a floor): run: python3 -m pytest tests/test_widgets.py · the index links no test to: Widget::__init__ (the tests named here may still reach it). Run the tests before finishing, or say why not.",
  );
  assert.match(
    renderGate(GATE, { testExists: () => true }),
    /Foo::bar \(the tests named here may still reach them\)\. /,
  );
  // With no test to run there is none to hedge about, and nothing to add: no run measured the code unexecuted.
  for (const none of [renderGate({ ...GATE, tests: [] }), renderGate(GATE, { testExists: () => false })]) {
    assert.match(none, /the index links no test to: MergeTreeData::renameTempPartAndAdd, Foo::bar[ .]/);
    assert.doesNotMatch(none, /may still reach|Add a test/);
  }
  assert.equal(
    renderGate({ ...GATE, tests: [] }),
    "Loom test gate (graph is a floor): the index links no test to: MergeTreeData::renameTempPartAndAdd, Foo::bar.",
  );
  for (const text of [renderGate(GATE), renderGate(built), renderGate({ ...GATE, tests: [] })]) {
    assert.doesNotMatch(text, /no test reaches/);
  }
});

test("a definition a coverage run measured unexecuted asks for a test", () => {
  const measured = {
    obligations: true,
    tests: [{ test_path: "tests/test_a.py", why: "static_call", run: "pytest tests/test_a.py" }],
    untested: [
      { qualified_name: "pkg.mod::Parser::reset", commit: "0123456789abcdef0123456789abcdef01234567" },
      { qualified_name: "pkg.mod::Parser::close", commit: "0123456789abcdef0123456789abcdef01234567" },
    ],
    untested_total: 2,
    unlinked: [{ qualified_name: "pkg.mod::helper" }],
  };
  assert.equal(
    renderGate(measured),
    "Loom test gate: run: pytest tests/test_a.py · the index links no test to: pkg.mod::helper (the tests named here may still reach it) · measured unexecuted at 0123456789: Parser::reset, Parser::close. Run the tests before finishing, or say why not. Add a test that runs the unexecuted code, or say why none is needed.",
  );
  // A row that names no run is no measurement, and asks for nothing.
  assert.equal(renderGate({ obligations: true, untested: [{ qualified_name: "pkg.mod::f" }] }), "");
  // Runs at two commits name neither.
  const mixed = { ...measured, untested: [measured.untested[0], { qualified_name: "pkg.mod::f", commit: "ffff" }] };
  assert.match(renderGate(mixed), /measured unexecuted: Parser::reset, pkg\.mod::f\. /);
});

test("plain pytest runs fold into one command; a run with an option stays its own", () => {
  const gate = (runs) => ({ obligations: true, tests: runs.map((run) => ({ run })) });
  assert.equal(
    renderGate(gate(["pytest tests/a.py", "pytest tests/b.py"]), { python: "python3" }),
    "Loom test gate: run: python3 -m pytest tests/a.py tests/b.py. Run the tests before finishing, or say why not.",
  );
  assert.match(
    renderGate(gate(["npm test", "pytest tests/a.py", "pytest -k slow tests/c.py", "pytest tests/b.py::test_x"])),
    /run: npm test ; pytest tests\/a\.py tests\/b\.py::test_x ; pytest -k slow tests\/c\.py\. /,
  );
  assert.match(renderGate(gate(["pytest tests/a.py"])), /run: pytest tests\/a\.py\. /);
});

test("render caps and counts the overflow", () => {
  const many = {
    obligations: true,
    tests: Array.from({ length: 6 }, (_, i) => ({ run: `pytest t${i}.py` })),
    untested: [],
    untested_total: 0,
  };
  const text = renderGate(many, { maxRuns: 2 });
  assert.match(text, /run: pytest t0\.py t1\.py \(\+4 more\)\./);
  const mixed = { ...many, tests: Array.from({ length: 6 }, (_, i) => ({ run: `npm test t${i}` })) };
  assert.match(renderGate(mixed, { maxRuns: 2 }), /run: npm test t0 ; npm test t1 \(\+4 more\)\./);
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
  assert.match(text, /the index links no test to:/);
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
  // The shell tool under each name `normalizeToolName` folds onto Bash for a function call; another tool that
  // names a path is no change.
  const named = (name) => [
    {
      type: "response_item",
      payload: { type: "function_call", name, arguments: '{"command":["bash","-lc","sed -i s/x/y/ src/b.py"]}' },
    },
  ];
  for (const name of ["exec", "exec_command", "shell", "shell_command", "container.exec"]) {
    assert.deepEqual(sessionScope(DIRTY, named(name), { root: ROOT }), ["src/b.py"], name);
  }
  assert.deepEqual(sessionScope(DIRTY, named("read_file"), { root: ROOT }), []);
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

test("a file last written before the session began is not the session's change", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-since-"));
  try {
    for (const f of ["old.py", "new.py"]) fs.writeFileSync(path.join(dir, f), "x = 1\n");
    const began = Date.parse("2001-09-09T01:46:40.000Z");
    fs.utimesSync(path.join(dir, "old.py"), new Date(began - 1000), new Date(began - 1000));
    fs.utimesSync(path.join(dir, "new.py"), new Date(began + 1000), new Date(began + 1000));
    const rows = [
      { type: "user", message: { content: "hi" } },
      { type: "assistant", timestamp: "2001-09-09T01:46:40.000Z" },
    ];
    assert.equal(sessionStart(rows), began);
    assert.equal(sessionStart([{ type: "user" }, { timestamp: "nope" }]), 0);
    assert.equal(sessionStart(undefined), 0);
    // A deleted file has no time and stays; an unknown start keeps every path.
    assert.deepEqual(writtenSince(dir, ["old.py", "new.py", "gone.py"], began), ["new.py", "gone.py"]);
    assert.deepEqual(writtenSince(dir, ["old.py", "new.py"], 0), ["old.py", "new.py"]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

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
    untested: [{ qualified_name: "server.naming::validate_namespace_name", commit: "abc1234" }],
  });
  assert.match(text, /measured unexecuted at abc1234: server\.naming::validate_namespace_name/);
  assert.match(text, /Add a test that runs the unexecuted code, or say why none is needed\.$/);
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
import { declinesTests, lastCodeChange, newestChange, runAnswered, testedSince } from "../lib/test-gate.mjs";
import { cachedUnindexed, cachePathFor } from "../lib/repo-scope.mjs";
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
  unlinked: [{ qualified_name: "m::f" }],
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
  // A second command that does anything before its pytest keeps only its own run:
  // a file put back, moved or deleted leaves none newer than the first run.
  for (const first of [
    "git checkout -- src/b.py && ",
    "mv src/helper.py src/util.py; ",
    "rm src/helper.py && ",
    "cd /r && ",
  ]) {
    const later = [...runs.slice(0, 2), bash(`${first}pytest tests/test_b.py -q`, "2 passed in 0.10s")];
    assert.equal(answered(later, two), false, first);
    assert.equal(answered(later, { ...RUN_GATE, tests: two.tests.slice(1) }), true, first);
  }
  const second = bash("CI=1 timeout 60 python -m pytest tests/test_b.py -q", "2 passed in 0.10s");
  assert.equal(answered([...runs.slice(0, 2), second], two), true);
  // A run of the checkout root names no path below it.
  for (const whole of ["pytest -q", "pytest . -q", "pytest /r -q"]) {
    assert.equal(answered([edit(), bash(whole, "3 passed in 0.10s")]), false, whole);
  }
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
    "pytest tests -q 2>/dev/null.log",
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
    "pytest tests -q 2>/dev/null | tail -5",
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

// Output of a suite that fails 8 tests with or without the change, and of a run that stopped at its first failure.
const LAST_FAILED = "FAILED tests/test_io.py::test_reads_every_row - Deprec...\n";
const BASE = `${LAST_FAILED}8 failed, 466 passed, 10 skipped in 1.01s`;
const CHANGED = `${LAST_FAILED}8 failed, 467 passed, 10 skipped in 0.81s`;
const STOPPED =
  "FAILED tests/test_basic.py::test_reads_one_row - AssertionError: ...\n" +
  "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n" +
  "1 failed, 38 passed in 0.37s";

test("a run that printed its failures answers the gate, unless it stopped at the first one", () => {
  assert.equal(answered([edit(), bash(SUITE, FAILED)]), true);
  assert.equal(answered([edit(), bash(SUITE, PASSED), bash(SUITE, FAILED)]), true);
  assert.equal(answered([edit(), bash("python -m pytest tests -q 2>&1 | tail -3", CHANGED)]), true);
  assert.equal(answered([edit(), bash("pytest tests -q", "400 passed, 5 errors in 1.60s")]), true);
  assert.equal(answered([edit(), bash("pytest tests -q", "3 failed in 0.10s")]), true);
  // The run never reached the files after the failure.
  for (const flag of ["-x", "-qx", "--exitfirst", "--maxfail=1", "--maxfail 2"]) {
    const stops = `python -m pytest tests -q ${flag} 2>&1 | tail -3`;
    assert.equal(answered([edit(), bash(stops, STOPPED)]), false, flag);
    assert.equal(answered([edit(), bash(stops, "38 passed, 1 error in 0.37s")]), false, flag);
    assert.equal(answered([edit(), bash(stops, PASSED)]), true, flag);
  }
  // A limit set outside pytest's arguments shows in pytest's own line; so does --sw set in a configuration file.
  assert.equal(answered([edit(), bash("PYTEST_ADDOPTS=-x python -m pytest tests -q 2>&1 | tail -3", STOPPED)]), false);
  const stepwise =
    "!!!!!!!! Interrupted: Test failed, continuing from this test next run. !!!!!!!!!\n1 failed, 38 passed in 0.37s";
  assert.equal(answered([edit(), bash("pytest tests -q", stepwise)]), false);
  // A named file that failed to import is one error among the tests that ran.
  assert.equal(
    answered([edit(), bash("pytest tests -q --continue-on-collection-errors", "400 passed, 1 error in 1.60s")]),
    false,
  );
  // A collection that stopped at an error ran no test.
  for (const none of ["1 error in 0.12s", "2 skipped, 1 error in 0.20s"]) {
    assert.equal(answered([edit(), bash("pytest tests -q", none)]), false, none);
  }
});

test("a command's runs count from the first one that only runs and output follow", () => {
  const two = {
    ...RUN_GATE,
    tests: [...RUN_GATE.tests, { test_path: "tests/test_b.py", run: "pytest tests/test_b.py" }],
  };
  const onlyB = { ...RUN_GATE, tests: two.tests.slice(1) };
  // Without the change, then with it: the last run tested the tree as it stands.
  const both =
    "git stash -q; python -m pytest tests -q 2>&1 | tail -2; git stash pop -q; python -m pytest tests -q 2>&1 | tail -2";
  assert.equal(answered([edit(), bash(both, `${BASE}\n${CHANGED}`)]), true);
  assert.equal(answered([edit(), bash(`cd /r; ${both}`, `${BASE}\n${CHANGED}`)]), true);
  assert.equal(answered([edit(), bash(both, CHANGED)]), false);
  // The run before the pop tested another tree, and the pop forgets every earlier run.
  const split = "pytest tests/test_a.py -q; git stash pop -q; pytest tests/test_b.py -q";
  const output = "3 passed in 0.10s\n2 passed in 0.10s";
  assert.equal(answered([edit(), bash(split, output)]), false);
  assert.equal(answered([edit(), bash(split, output)], onlyB), true);
  assert.equal(
    answered([edit(), bash("pytest tests/test_a.py -q", "3 passed in 0.10s"), bash(split, output)], two),
    false,
  );
  // With the change, then without it: the first run printed no summary and the pop came after the last.
  const backwards =
    "python -m pytest tests -q 2>&1 | grep FAILED; git stash -q; python -m pytest tests -q 2>&1 | tail -1; git stash pop -q";
  assert.equal(answered([edit(), bash(backwards, BASE)]), false);
  assert.equal(answered([edit(), bash("python -m pytest tests -q 2>&1 | tail -3; git diff --stat", CHANGED)]), false);
  // A move between two runs leaves the later run's paths unknown.
  assert.equal(
    answered([edit(), bash("pytest tests -q; git stash pop -q; cd sub; pytest tests -q", output)], {
      ...RUN_GATE,
      tests: [{ test_path: "sub/tests/test_a.py", run: "pytest sub/tests/test_a.py" }],
    }),
    false,
  );
});

test("a run of a subset or of another file answers nothing", () => {
  assert.equal(answered([edit(), bash("pytest tests -q -k new", "1 passed, 4 deselected in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests/test_a.py::test_new -q", "1 passed in 0.10s")]), false);
  assert.equal(answered([edit(), bash("pytest tests --ignore=tests/test_a.py -q", "9 passed in 0.10s")]), false);
  // Flags that leave tests out; --lf does it at collection, so its summary counts nothing as deselected.
  for (const subset of ["--lf", "--last-failed", "--sw", "--stepwise", "--deselect tests/test_a.py::test_x"]) {
    assert.equal(answered([edit(), bash(`pytest tests ${subset} -q`, "1 passed in 0.10s")]), false, subset);
  }
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
  // A quoted value may hold several words, and the last would read as a path.
  assert.equal(answered([edit(), bash('pytest src -q -m "not tests"', "3 passed in 0.10s")]), false);
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
  const untested = { ...RUN_GATE, tests: [], untested: [{ qualified_name: "m::f", commit: "abc1234" }] };
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

test("any test run that finished since the change counts, whatever it ran and however it went", () => {
  const since = Date.parse(edit()[0].timestamp);
  for (const command of [
    SUITE,
    "git stash; pytest tests; git stash pop",
    "pytest tests/test_other.py -x 2>&1 | grep FAILED",
    "sed -i s/1/2/ src/a.py && .venv/bin/python -m unittest discover",
    "npx vitest run src/a.test.ts",
    "go test ./pkg/...",
    "go test ./...",
    "make test-unit",
    "npm run test",
    "node --test",
    "node --test tests/a.test.mjs",
    "gradle :app:test",
    "./gradlew testDebugUnitTest",
    "make unit-tests",
    "cargo nextest run",
    "tests/clickhouse-test 01234_x",
    "make testacc",
    "make testall",
    "npm run testci",
    "./gradlew integrationTest",
    "gradle connectedAndroidTest",
    // A run after an install, in its own simple command.
    "npm ci && npm test",
    "pip install -e . && pytest",
    "yarn add -D jest; yarn jest",
    "for i in 1 2 3; do pytest tests; done",
    "npx --no-install jest",
    "docker run --rm app pytest",
    "pip install -e . \\\n  && pytest",
    // A line continuation joins the run to its tool.
    "make \\\n  test",
  ]) {
    assert.equal(testedSince(bash(command, FAILED), since), true, command);
    assert.equal(testedSince(call("PowerShell", { command }), since), true, command);
  }
  // A command that reads or writes a test, or installs, removes or updates a
  // package, runs none; neither does a build tool whose argument holds `test`
  // inside a word or at the start of a path's or a package's name, one that
  // builds the tests, or one told to leave them out.
  for (const command of [
    HEREDOC,
    "sed -n 1,40p tests/test_a.py",
    "cat pytest.ini",
    "pip install pytest",
    "git diff",
    "npm install x@latest",
    "go install y@latest",
    "make latest",
    "node scripts/attest.js",
    "npm install @testing-library/react",
    "go get github.com/stretchr/testify",
    "npm install @playwright/test",
    "npm install -D node-test",
    "npm install --save-dev jest",
    "yarn add -D jest",
    "pnpm add -D vitest",
    "pip install -U pytest",
    "pip install 'pytest>=8'",
    "uv add --dev pytest",
    "npm i -D @types/jest",
    "npm uninstall jest",
    "npm rm jest",
    "yarn remove jest",
    "pnpm remove vitest",
    "npm update jest",
    "yarn upgrade jest",
    "poetry update pytest",
    "pip download pytest",
    "composer require --dev phpunit/phpunit",
    "pip install \\\n  pytest",
    "npm install --save-dev \\\n  jest \\\n  vitest",
    "go vet ./internal/testutil/...",
    "go build ./cmd/testserver",
    "go generate ./testdata/...",
    "bazel build //testing/...",
    "bazel build //pkg:testlib",
    "node scripts/testbed.js",
    "cargo build -p testcontainers",
    "gradle build -x test",
    "./gradlew build -x integrationTest",
    "./gradlew compileTestJava",
    "./gradlew :app:compileDebugUnitTestKotlin",
    "./gradlew processTestResources",
    "gradle build -PskipTests",
    "gradle assemble -DskipTests",
    "mvn package -Dmaven.test.skip=true",
    // A build tool's target of a verb's name hides the run after it.
    "make install test",
    "make ci test",
    "mvn clean install test",
  ]) {
    assert.equal(testedSince(bash(command, ""), since), false, command);
  }
  assert.equal(testedSince(call("Read", { command: SUITE }), since), false);
  assert.equal(testedSince(bash(SUITE, PASSED, { isSidechain: true }), since), false);
  // A row of another type that copies the call is not the session's call.
  assert.equal(testedSince([{ ...bash(SUITE, PASSED)[0], type: "api-request-blob" }], since), false);
  // A run that finished before the change does not count; one with no dated result does.
  const before = bash(SUITE, PASSED);
  assert.equal(testedSince(before, Date.parse(before[1].timestamp) + 1), false);
  assert.equal(testedSince(before.slice(0, 1), Date.parse(before[1].timestamp) + 1), true);
  // A Codex rollout's shell call counts by its output row's time.
  const item = (payload, ms) => ({ type: "response_item", timestamp: new Date(ms).toISOString(), payload });
  const rollout = [
    item(
      { type: "function_call", name: "shell", call_id: "c1", arguments: '{"command":["pytest","tests","-q"]}' },
      1e12,
    ),
    item({ type: "function_call_output", call_id: "c1", output: "3 passed in 0.10s" }, 1e12 + 1000),
  ];
  assert.equal(testedSince(rollout, 1e12 + 500), true);
  assert.equal(testedSince(rollout, 1e12 + 1500), false);
  // Its arguments hold an argv, a shell wrapper's script or one string.
  const shell = (args) => [item({ type: "function_call", name: "shell", call_id: "c2", arguments: args }, 1e12)];
  for (const args of [
    '{"command":["go","test","./..."]}',
    '{"command":["npm","test"]}',
    '{"command":["bash","-lc","make test-unit"]}',
    '{"cmd":"npm run test"}',
    "go test ./...",
  ]) {
    assert.equal(testedSince(shell(args), 1e12), true, args);
  }
  assert.equal(testedSince(shell('{"command":["npm","install","x@latest"]}'), 1e12), false);
  // The shell tool counts under each name the client gives it; another tool that names a runner does not.
  const named = (name) => [
    item({ type: "function_call", name, call_id: "c3", arguments: '{"command":"pytest -q"}' }, 1e12),
  ];
  for (const name of ["exec", "exec_command", "shell", "shell_command", "local_shell", "container.exec"]) {
    assert.equal(testedSince(named(name), 1e12), true, name);
  }
  assert.equal(testedSince(named("apply_patch"), 1e12), false);
  assert.equal(testedSince([], 0), false);
});

test("a tool and its argument are read up to 256 characters apart, and a long line in time linear in its length", () => {
  const ran = (command) => testedSince(bash(command, ""), 0);
  assert.equal(ran(`make ${"a".repeat(255)} test`), true);
  assert.equal(ran(`make ${"a".repeat(256)} test`), false);
  // A verb further back than that names no package.
  assert.equal(ran(`pip install ${"a".repeat(249)} pytest`), false);
  assert.equal(ran(`pip install ${"a".repeat(250)} pytest`), true);
  // Two lines of 20,000 characters. The first cost over 15 s of CPU with no
  // bound; the second, where each tool has a verb and many `tests` within
  // reach, is the costliest shape found with one.
  const before = process.cpuUsage();
  assert.equal(ran(`x install ${"make test ".repeat(2000)}`), true);
  assert.equal(ran(`${" npm".repeat(44)} rm${" tests".repeat(18)}`.repeat(70)), false);
  assert.ok(process.cpuUsage(before).user < 2e6);
});

test("the last code change is the most recently written code file that can be read", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-last-code-"));
  try {
    for (const [file, at] of [
      ["a.py", 1e12],
      ["b.py", 1e12 + 5000],
      ["constraints.txt", 1e12 + 6000],
      ["requirements/dev.txt", 1e12 + 6000],
      ["tests/fixtures/expected.txt", 1e12 + 6000],
      ["testdata/golden.txt", 1e12 + 6000],
      ["docs/requirements.txt", 1e12 + 6000],
      ["docs/releases/5.2.txt", 1e12 + 9000],
      ["CHANGES.txt", 1e12 + 9000],
      ["README.txt", 1e12 + 9000],
      ["README.md", 1e12 + 9000],
      ["notes.markdown", 1e12 + 9000],
      ["guide.rst", 1e12 + 9000],
      ["manual.asciidoc", 1e12 + 9000],
      ["CHANGELOG", 1e12 + 9000],
    ]) {
      fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
      fs.writeFileSync(path.join(dir, file), "x\n");
      fs.utimesSync(path.join(dir, file), new Date(at), new Date(at));
    }
    assert.equal(lastCodeChange(dir, ["b.py", "a.py", "deleted.py"]), 1e12 + 5000);
    // Documentation and prose are left out: by extension, by a changelog's name, and a .txt file by a note's
    // name or under a docs directory.
    for (const file of [
      "README.md",
      "notes.markdown",
      "guide.rst",
      "manual.asciidoc",
      "CHANGELOG",
      "docs/releases/5.2.txt",
      "CHANGES.txt",
      "README.txt",
    ]) {
      assert.equal(lastCodeChange(dir, ["a.py", "b.py", file]), 1e12 + 5000, file);
    }
    // Any other .txt file is code: a requirements file, also under docs, a test fixture.
    for (const file of [
      "constraints.txt",
      "requirements/dev.txt",
      "tests/fixtures/expected.txt",
      "testdata/golden.txt",
      "docs/requirements.txt",
    ]) {
      assert.equal(lastCodeChange(dir, ["a.py", file]), 1e12 + 6000, file);
    }
    assert.equal(lastCodeChange(dir, ["README.md", "deleted.py"]), 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("the hook holds the stop of a session that ran no test since its last code change; LOOM_TEST_GATE=block also holds one whose named tests did not run on the final tree", async () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-hook-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-gate-hook-tmp-")));
  for (const f of ["src/a.py", "src/b.py", "tests/test_a.py", "docs/guide.md"]) {
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
  const ranBefore = bash(SUITE, PASSED);
  const change = call("Edit", { file_path: path.join(dir, "src/a.py") }, "updated");
  // The change is as old as the session's first row, until something rewrites a file.
  const touch = (file, when) => fs.utimesSync(path.join(dir, file), when, when);
  touch("src/a.py", new Date(change[0].timestamp));
  fs.writeFileSync(
    cachePathFor(dir, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push(req.url);
    req.resume();
    req.on("end", () => res.end(JSON.stringify(req.url === "/v1/code.test_gate" ? RUN_GATE : {})));
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const hook = fileURLToPath(new URL("../hooks/loom-test-gate.mjs", import.meta.url));
  const stop = (session, rows, mode = "block") =>
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
          ...(mode ? { LOOM_TEST_GATE: mode } : {}),
        }),
        stdio: ["pipe", "pipe", "ignore"],
      });
      let out = "";
      child.stdout.on("data", (c) => (out += c));
      child.on("close", () => resolve(out));
      child.stdin.end(JSON.stringify({ cwd: dir, session_id: session, transcript_path: transcript }));
    });
  try {
    const ran = [change, bash(SUITE, PASSED)];
    const no = [said("Fix f. Do not run the test suite."), change];
    // The default: a change with no test run since it is held once, with the tests to run.
    assert.match(await stop("d-never-ran", [change], ""), /"decision":"block".*pytest tests\/test_a\.py/);
    assert.equal(await stop("d-never-ran", [change], ""), "");
    assert.match(await stop("d-ran-before", [ranBefore, change], ""), /"decision":"block"/);
    assert.match(await stop("d-only-read", [change, bash("cat tests/test_a.py", "")], ""), /"decision":"block"/);
    // Any run since the change passes: one that failed, ran another file, or was followed by more commands.
    const asked = requests.length;
    assert.equal(await stop("d-ran", ran, ""), "");
    assert.equal(await stop("d-failed", [change, bash("pytest tests/test_b.py -x | grep FAILED", "")], ""), "");
    assert.equal(await stop("d-then-more", [...ran, bash("rm src/helper.py", ""), bash("git diff", "")], ""), "");
    // A documentation file written after the run is not a code change.
    const later = (file) => {
      const edited = call("Edit", { file_path: path.join(dir, file) }, "updated");
      fs.appendFileSync(path.join(dir, file), "def h():\n    return 3\n");
      touch(file, new Date(edited[1].timestamp));
      return edited;
    };
    assert.equal(await stop("d-doc-after-run", [...ran, later("docs/guide.md")], ""), "");
    assert.equal(requests.length, asked, "a stop that passes on a test run asks the server nothing");
    // A second code file written after the run is: the run saw the first one only.
    assert.match(await stop("d-run-between", [...ran, later("src/b.py")], ""), /"decision":"block"/);
    execFileSync("git", ["checkout", "-q", "--", "src/b.py", "docs/guide.md"], { cwd: dir });
    assert.equal(await stop("d-ruled-out", no, ""), "");
    assert.match(await stop("never-ran", [change]), /"decision":"block".*pytest tests\/test_a\.py/);
    assert.equal(await stop("never-ran", [change]), "");
    // A Codex rollout that changed the file through its shell tool is held in either mode, under each name of
    // that tool; a call of another tool that names the file is no change.
    const rollout = (name) => [
      {
        type: "response_item",
        payload: { type: "function_call", name, arguments: '{"command":"sed -i s/1/2/ src/a.py"}' },
      },
    ];
    assert.match(await stop("codex-block", rollout("shell_command")), /"decision":"block"/);
    assert.match(await stop("codex-default", rollout("shell_command"), ""), /"decision":"block"/);
    assert.equal(await stop("codex-other-tool", rollout("read_file")), "");
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
    // A tree left dirty before the session: reading or running the file changes nothing.
    touch("src/a.py", new Date(1e12 - 1000));
    assert.equal(
      await stop("only-read", [bash("cat src/a.py", "def f():"), bash("grep -n g src/a.py", "3:def g():")]),
      "",
    );
  } finally {
    server.closeAllConnections();
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

// --- the default: the tests are named on the call that changed the file

test("a checkout the cache says is not indexed is known without git; no other entry says so", () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-unindexed-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-unindexed-tmp-")));
  const below = path.join(dir, "src", "pkg");
  fs.mkdirSync(below, { recursive: true });
  fs.mkdirSync(path.join(dir, ".git"));
  const cached = (entry) => {
    fs.writeFileSync(cachePathFor(dir, tmp), JSON.stringify(entry));
    return cachedUnindexed(below, { tmp });
  };
  try {
    assert.equal(cachedUnindexed(below, { tmp }), false);
    assert.equal(cached({ ts: Date.now(), indexed: false }), true);
    assert.equal(cachedUnindexed(dir, { tmp }), true);
    // The server did not answer, the answer is old, or the checkout is indexed.
    assert.equal(cached({ ts: Date.now(), indexed: false, unresolved: true }), false);
    assert.equal(cached({ ts: Date.now() - 3_600_000, indexed: false }), false);
    assert.equal(cached({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x" }), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("a changed file's tests ride the tool call that changed it, once", async () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-advice-")));
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "loom-advice-tmp-")));
  const sources = ["src/a.py", "src/b.py", "src/d.py", "src/f.py", "src/j.py", "src/k.py", "src/old.py"];
  const tests = ["a", "b", "c", "e", "g", "h", "i"].map((n) => `tests/test_${n}.py`);
  for (const f of [...sources, ...tests]) {
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
  fs.writeFileSync(
    cachePathFor(dir, tmp),
    JSON.stringify({ ts: Date.now(), indexed: true, org: "o", namespace: "code-x", repo: "x/y", prefixes: [] }),
  );
  // The session began a minute ago; src/old.py was left dirty an hour before it.
  const transcript = path.join(tmp, "t.jsonl");
  fs.writeFileSync(
    transcript,
    JSON.stringify({ type: "user", timestamp: new Date(Date.now() - 60_000).toISOString() }) + "\n",
  );
  const write = (file, text, when = new Date()) => {
    fs.appendFileSync(path.join(dir, file), text);
    fs.utimesSync(path.join(dir, file), when, when);
  };
  write("src/old.py", "def parked():\n    return 0\n", new Date(Date.now() - 3_600_000));
  const asked = [];
  // How many of the next requests get a 500.
  let failing = 0;
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const body = JSON.parse(raw || "{}");
      asked.push([req.url, ...(body.files || [])].join(" "));
      if (failing-- > 0) {
        res.statusCode = 500;
        return res.end("{}");
      }
      const stem = (body.files || []).map((f) => (f.match(/^src\/(\w+)\.py$/) || [])[1]).filter(Boolean);
      const answer = {
        "/v1/code.test_gate": {
          obligations: true,
          tests: [
            ...(body.files || [])
              .filter((f) => f.startsWith("tests/"))
              .map((f) => ({ test_path: f, why: "changed_test", run: `pytest ${f}` })),
            ...stem.map((n) => ({
              test_path: `tests/test_${n}.py`,
              why: "test_map",
              run: `pytest tests/test_${n}.py`,
            })),
          ],
          unlinked: stem.length ? [{ qualified_name: "m::g" }] : [],
        },
        "/v1/code.impact": {
          touched_symbols: [{ qualified_name: "m::g" }],
          impacted_symbols: [{ qualified_name: "m::caller", path: "src/c.py" }],
          impacted_tests: [{ test_path: "tests/test_b.py" }],
        },
      }[req.url];
      res.end(JSON.stringify(answer || {}));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const run = (script, event, payload, env = {}) =>
    new Promise((resolve) => {
      const child = spawn(
        process.execPath,
        [fileURLToPath(new URL(`../hooks/${script}.mjs`, import.meta.url)), event],
        {
          env: hostEnv({
            PATH: process.env.PATH,
            HOME: tmp,
            TMPDIR: tmp,
            LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
            LOOM_API_KEY: "t",
            ...env,
          }),
          stdio: ["pipe", "pipe", "ignore"],
        },
      );
      let out = "";
      child.stdout.on("data", (c) => (out += c));
      child.on("close", () => resolve(out ? JSON.parse(out) : null));
      child.stdin.end(JSON.stringify({ cwd: dir, session_id: "s", transcript_path: transcript, ...payload }));
    });
  const shell = (command, env) =>
    run("loom-test-gate", "PostToolUse", { tool_name: "Bash", tool_input: { command } }, env);
  const context = (out) => out?.hookSpecificOutput?.additionalContext ?? "";
  try {
    // A command that changes nothing, or only reads a file left dirty before the session, asks nothing.
    assert.equal(await shell("ls src; cat src/old.py"), null);
    assert.deepEqual(asked, []);
    // One command edits a source file and its test: one line, since the test is named once.
    write("src/a.py", "def g():\n    return 2\n");
    write("tests/test_a.py", "def test_g():\n    assert True\n");
    const edit = "python - <<'EOF'\nopen('src/a.py','a').write('x')\nopen('tests/test_a.py','a').write('y')\nEOF";
    const first = await shell(edit);
    assert.equal(first.hookSpecificOutput.hookEventName, "PostToolUse");
    assert.match(context(first), /^Loom tests for src\/a\.py: (?:\S+ -m )?pytest tests\/test_a\.py$/);
    assert.equal(first.decision, undefined);
    assert.deepEqual(asked.sort(), ["/v1/code.test_gate src/a.py", "/v1/code.test_gate tests/test_a.py"]);
    // Later commands that name the file, a test run among them, add nothing and ask nothing.
    assert.equal(await shell(edit), null);
    assert.equal(await shell("python -m pytest tests/test_a.py -q; git diff src/a.py"), null);
    assert.equal(asked.length, 2);
    // An edit tool's call carries the line beside the blast radius, which then counts no tests.
    write("src/b.py", "def g():\n    return 2\n");
    const edited = await run("loom-blast-radius", "PostToolUse", {
      tool_name: "Edit",
      tool_input: { file_path: path.join(dir, "src/b.py") },
    });
    assert.match(
      context(edited),
      /^Loom blast radius for src\/b\.py \(edited m::g\): 1 caller in 1 file: m::caller \(c\.py\)\. /,
    );
    assert.match(context(edited), / Loom tests for src\/b\.py: (?:\S+ -m )?pytest tests\/test_b\.py$/);
    assert.doesNotMatch(context(edited), /tests reaching it/);
    // The shell hook leaves an edit tool's call to that hook, and the file is asked about once.
    assert.equal(
      await run("loom-test-gate", "PostToolUse", {
        tool_name: "Edit",
        tool_input: { file_path: path.join(dir, "src/b.py") },
      }),
      null,
    );
    assert.equal(await shell("sed -i s/2/3/ src/b.py"), null);
    // The test of a file already named adds no second line when it changes; another test names itself.
    write("tests/test_b.py", "def test_g():\n    assert True\n");
    assert.equal(await shell("sed -i s/1/2/ tests/test_b.py"), null);
    write("tests/test_c.py", "def test_g():\n    assert True\n");
    assert.match(
      context(await shell("sed -i s/1/2/ tests/test_c.py")),
      /^Loom tests for tests\/test_c\.py: (?:\S+ -m )?pytest tests\/test_c\.py$/,
    );
    // A file with no test in the checkout gets the functions the index links no test to, once.
    write("src/d.py", "def g():\n    return 2\n");
    assert.equal(
      context(await shell("sed -i s/1/2/ src/d.py")),
      "Loom tests for src/d.py: the index links no test to m::g",
    );
    assert.equal(await shell("sed -i s/2/3/ src/d.py"), null);
    // After an edit tool that line names no test, so the radius keeps its own count.
    write("src/f.py", "def g():\n    return 2\n");
    const unlinked = await run("loom-blast-radius", "PostToolUse", {
      tool_name: "Edit",
      tool_input: { file_path: path.join(dir, "src/f.py") },
    });
    assert.match(
      context(unlinked),
      /tests reaching it: 1 \(test_b\.py\).* Loom tests for src\/f\.py: the index links no test to m::g$/,
    );
    // A test the command itself runs is not named on the result of that run, nor later.
    write("tests/test_e.py", "def test_g():\n    assert True\n");
    assert.equal(await shell("python -m pytest tests/test_e.py -q"), null);
    assert.ok(asked.includes("/v1/code.test_gate tests/test_e.py"), asked.join(", "));
    assert.equal(await shell("sed -i s/1/2/ tests/test_e.py"), null);
    // A command that runs one test of the file, runs it elsewhere or only prints its command did not run it here.
    for (const [n, command] of [
      ["g", "sed -i s/1/2/ tests/test_g.py && pytest tests/test_g.py::test_g"],
      ["h", "docker exec app pytest tests/test_h.py"],
      ["i", 'echo "pytest tests/test_i.py"'],
    ]) {
      write(`tests/test_${n}.py`, "def test_g():\n    assert True\n");
      assert.match(
        context(await shell(command)),
        new RegExp(`^Loom tests for tests/test_${n}\\.py: (?:\\S+ -m )?pytest tests/test_${n}\\.py$`),
        command,
      );
    }
    // A file the server gave no answer for is asked about at the next call that names it, once.
    failing = 1;
    write("src/j.py", "def g():\n    return 2\n");
    assert.equal(await shell("sed -i s/1/2/ src/j.py"), null);
    assert.equal(
      context(await shell("sed -i s/2/3/ src/j.py")),
      "Loom tests for src/j.py: the index links no test to m::g",
    );
    failing = 2;
    write("src/k.py", "def g():\n    return 2\n");
    assert.equal(await shell("sed -i s/1/2/ src/k.py"), null);
    assert.equal(await shell("sed -i s/2/3/ src/k.py"), null);
    assert.equal(await shell("sed -i s/3/4/ src/k.py"), null);
    assert.equal(asked.filter((a) => a.endsWith("src/k.py")).length, 2, asked.join(", "));
    failing = 0;
    // A comment-only change owes no test and stays unasked until it changes code.
    write("src/old.py", "# a note\n");
    execFileSync("git", ["checkout", "-q", "--", "src/old.py"], { cwd: dir });
    write("src/old.py", "# a note\n");
    assert.equal(await shell("echo '# a note' >> src/old.py"), null);
    assert.ok(!asked.some((a) => a.endsWith("src/old.py")), asked.join(", "));
    // This transcript shows no tool call, so the stop has no change of the session's to hold.
    assert.equal(await run("loom-test-gate", "Stop", {}), null);
    // LOOM_TEST_GATE=block: nothing at edit time, the radius counts its tests, the stop is held.
    const blocking = { LOOM_TEST_GATE: "block" };
    write("src/a.py", "def h():\n    return 3\n");
    assert.equal(
      await run(
        "loom-test-gate",
        "PostToolUse",
        { session_id: "b", tool_name: "Bash", tool_input: { command: edit } },
        blocking,
      ),
      null,
    );
    const radius = await run(
      "loom-blast-radius",
      "PostToolUse",
      { session_id: "b", tool_name: "Edit", tool_input: { file_path: path.join(dir, "src/a.py") } },
      blocking,
    );
    assert.match(context(radius), /tests reaching it: 1 \(test_b\.py\)/);
    assert.doesNotMatch(context(radius), /Loom tests for/);
    const stopped = await run("loom-test-gate", "Stop", { session_id: "b", transcript_path: "" }, blocking);
    assert.equal(stopped.decision, "block");
    // With no transcript nothing shows that the session ran no test: the default holds no stop.
    assert.equal(await run("loom-test-gate", "Stop", { session_id: "n", transcript_path: "" }), null);
    // LOOM_TEST_GATE=off: neither.
    const off = { LOOM_TEST_GATE: "off" };
    assert.equal(
      await run(
        "loom-test-gate",
        "PostToolUse",
        { session_id: "o", tool_name: "Bash", tool_input: { command: edit } },
        off,
      ),
      null,
    );
    assert.equal(await run("loom-test-gate", "Stop", { session_id: "o", transcript_path: "" }, off), null);
  } finally {
    server.closeAllConnections();
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
