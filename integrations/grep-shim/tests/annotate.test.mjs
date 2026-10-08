import { test } from "node:test";
import assert from "node:assert/strict";
import {
  annotatable,
  annotate,
  hitSuffixes,
  fileOperands,
  foldResults,
  locationBatches,
  parseHits,
  MARK,
} from "../lib/annotate.mjs";

test("annotatable: needs line numbers, refuses non path:line:text shapes", () => {
  assert.equal(annotatable(["-rn", "foo", "src/"]), true);
  assert.equal(annotatable(["-rni", "--include=*.cpp", "foo", "src/"]), true);
  assert.equal(annotatable(["--line-number", "-r", "foo", "."]), true);
  assert.equal(annotatable(["-r", "foo", "src/"]), false, "no -n: lines lack numbers");
  assert.equal(annotatable(["-rln", "foo", "src/"]), false, "-l lists files");
  assert.equal(annotatable(["-rnc", "foo", "src/"]), false, "-c counts");
  assert.equal(annotatable(["-rno", "foo", "src/"]), false, "-o prints fragments");
  assert.equal(annotatable(["-rnh", "foo", "src/"]), false, "-h drops the path");
  assert.equal(annotatable(["-rn", "--color=always", "foo", "src/"]), false, "escape codes");
  assert.equal(annotatable(["-rn", "-e", "-lc", "src/"]), true, "-e's value is not a flag cluster");
  assert.equal(annotatable(["-rn", "--", "-l", "src/"]), true, "after -- nothing is an option");
  assert.equal(annotatable(["-A3", "-rn", "foo", "src/"]), true, "-A3 carries its value inline");
  assert.equal(annotatable(["foo", "src/"], "rg"), false, "piped rg prints no line numbers without -n");
  assert.equal(annotatable(["-n", "foo", "src/"], "rg"), true);
  assert.equal(annotatable(["--column", "foo", "src/"], "rg"), true, "--column implies line numbers");
  assert.equal(annotatable(["-N", "foo", "src/"], "rg"), false);
  assert.equal(annotatable(["--json", "foo"], "rg"), false);
  assert.equal(annotatable(["-n", "-tcpp", "foo"], "rg"), true, "rg -t takes a value");
  assert.equal(annotatable(["-n", "-r", "N", "foo", "src/"], "rg"), true, "rg -r (--replace) takes a value");
  assert.equal(annotatable(["-rn", "foo", "src/"], "rg"), false, "rg -rn replaces with n: no -n given");
  assert.equal(annotatable(["-n", "-E", "latin1", "foo"], "rg"), true, "rg -E (--encoding) takes a value");
  assert.equal(annotatable(["-En", "foo", "src/"]), true, "grep -E is a flag");
  assert.equal(annotatable(["-p", "foo"], "rg"), false, "--pretty colours and groups");
  assert.equal(annotatable(["--heading", "foo"], "rg"), false, "headings drop the path from hit lines");
});

const toRel = (p) => (p.startsWith("src/") ? p : null);

test("parseHits: match lines only, real files only", () => {
  const lines = [
    "src/A.cpp:10:    foo();",
    "src/A.cpp-11-    bar();", // context line
    "--",
    "docs/x.md:3:foo", // outside the repo/index -> null
    "src/B.cpp:7:foo: not a number",
    "12:foo", // no path
  ];
  assert.deepEqual(parseHits(lines, toRel), [
    { idx: 0, rel: "src/A.cpp", line: 10 },
    { idx: 4, rel: "src/B.cpp", line: 7 },
  ]);
});

test("locationBatches: distinct, ordered, chunked, capped", () => {
  const hits = [];
  for (let i = 0; i < 1300; i++) hits.push({ idx: i, rel: "src/A.cpp", line: (i % 1100) + 1 });
  const b = locationBatches(hits);
  assert.equal(b.length, 5, "max 5 batches");
  assert.equal(b[0].length, 200);
  assert.equal(b[0][0], "src/A.cpp:1");
  assert.equal(b.flat().length, 1000, "1100 distinct, first 1000 sent");
});

test("annotate: suffix on resolved hits only, everything else untouched", () => {
  const lines = ["src/A.cpp:10:    foo();", "src/A.cpp-11-    bar();", "src/B.cpp:7:foo"];
  const hits = parseHits(lines, toRel);
  const scopes = foldResults(new Map(), [
    { path: "src/A.cpp", line: 10, qualified_name: "DB::A::run" },
    { path: "src/B.cpp", line: 7, qualified_name: "" },
  ]);
  const { lines: out, annotated } = annotate(lines, hits, scopes);
  assert.equal(annotated, 1);
  assert.equal(out[0], `src/A.cpp:10:    foo();${MARK}DB::A::run`);
  assert.equal(out[1], lines[1]);
  assert.equal(out[2], lines[2]);
  assert.equal(out.length, lines.length);
  assert.deepEqual([...hitSuffixes(hits, scopes)], [[0, `${MARK}DB::A::run`]], "one suffix, keyed by line index");
});

test("fileOperands: positionals after the pattern, option values excluded", () => {
  assert.deepEqual(fileOperands(["-n", "foo", "src/a.cpp"]), ["src/a.cpp"]);
  assert.deepEqual(fileOperands(["-rn", "foo", "src/"]), ["src/"]);
  assert.deepEqual(fileOperands(["-n", "-e", "foo", "src/a.cpp"]), ["src/a.cpp"], "-e carries the pattern");
  assert.deepEqual(fileOperands(["-ne", "foo", "src/a.cpp"]), ["src/a.cpp"], "-e at the end of a cluster");
  assert.deepEqual(fileOperands(["-n", "--regexp=foo", "a.cpp", "b.cpp"]), ["a.cpp", "b.cpp"]);
  assert.deepEqual(fileOperands(["-n", "-A", "3", "foo", "src/a.cpp"]), ["src/a.cpp"], "-A's value is not an operand");
  assert.deepEqual(fileOperands(["-n", "--include", "*.cpp", "-r", "foo", "src/"]), ["src/"]);
  assert.deepEqual(
    fileOperands(["-n", "foo", "--", "-weird.cpp"]),
    ["-weird.cpp"],
    "after -- everything is an operand",
  );
  assert.deepEqual(fileOperands(["-n", "foo"]), [], "stdin: no operand");
  assert.deepEqual(fileOperands(["-n", "foo", "-"]), ["-"]);
  assert.deepEqual(fileOperands(["-tcpp", "foo", "src/a.cpp"], "rg"), ["src/a.cpp"], "rg -t takes a value");
  assert.deepEqual(
    fileOperands(["-r", "X", "foo", "src/a.cpp"], "rg"),
    ["src/a.cpp"],
    "rg -r's value is not the pattern",
  );
  assert.deepEqual(fileOperands(["-rn", "foo", "src/a.cpp"]), ["src/a.cpp"], "grep -r is a flag");
});

test("single-file shape: line:text hits take the path from the operand", () => {
  const lines = ["12:    foo();", "13-    bar();", "--", "40:foo: not a path", "not a hit"];
  const toRelOne = (p) => (p === "src/a.cpp" ? "src/a.cpp" : null);
  assert.deepEqual(parseHits(lines, toRelOne, "src/a.cpp"), [
    { idx: 0, rel: "src/a.cpp", line: 12 },
    { idx: 3, rel: "src/a.cpp", line: 40 },
  ]);
  assert.deepEqual(
    parseHits(lines, () => null, "docs/x.md"),
    [],
    "operand outside the repo: nothing",
  );
  assert.equal(annotatable(["-n", "foo", "src/a.cpp"], "grep", { singleFile: true }), true);
  assert.equal(
    annotatable(["-nh", "foo", "src/a.cpp"], "grep", { singleFile: true }),
    true,
    "-h is fine: path is the operand",
  );
  assert.equal(annotatable(["-nh", "foo", "src/a.cpp", "src/b.cpp"]), false, "-h with several files drops the path");
  assert.equal(annotatable(["-c", "foo", "src/a.cpp"], "grep", { singleFile: true }), false, "counts stay untouched");
});

import { GRAPH_HEADER, collapsePaths, renderGraphBlock, stripGraphBlock } from "../lib/annotate.mjs";

const NEIGHBOURS = [
  {
    symbol_id: "a",
    qualified_name: "DB::Foo::bar",
    path: "src/Interpreters/Foo.cpp",
    module: "src/Interpreters",
    callers: ["DB::One::run", "DB::Two::run"],
    callees: ["DB::Helper::go"],
    tests: ["tests/queries/0_stateless/0001_foo.sql"],
    epistemic: "exact",
    causes: [],
  },
  {
    symbol_id: "b",
    qualified_name: "DB::Foo::bar::$lambda",
    path: "src/Interpreters/Foo.cpp",
    module: "src/Interpreters",
    callers: [],
    callees: [],
    tests: [],
    epistemic: "exact",
    causes: [],
  }, // nothing to relate: no line
  {
    symbol_id: "c",
    qualified_name: "DB::baz",
    path: "src/Other.cpp",
    module: "src",
    callers: [],
    callees: ["DB::qux"],
    tests: [],
    epistemic: "lower_bound",
    causes: ["unresolved_calls"],
  },
  // two anchors in one file: the file's tests are named once, collapsed by directory
  {
    symbol_id: "d",
    qualified_name: "DB::Foo::run",
    path: "src/Interpreters/Foo.cpp",
    module: "src/Interpreters",
    callers: ["DB::Main::go"],
    callees: [],
    tests: [
      "tests/queries/0_stateless/0001_foo.sql",
      "tests/queries/0_stateless/0002_foo.sh",
      "tests/integration/test_foo/test.py",
    ],
    epistemic: "exact",
    causes: [],
  },
];

test("renderGraphBlock: one line per related function, header first, lower bound named", () => {
  const block = renderGraphBlock(NEIGHBOURS);
  const lines = block.split("\n");
  assert.equal(lines[0], GRAPH_HEADER);
  assert.equal(
    lines[1],
    "  DB::Foo::bar [src/Interpreters] — callers: DB::One::run, DB::Two::run; callees: DB::Helper::go; tests: tests/queries/0_stateless/0001_foo.sql",
  );
  assert.equal(lines[2], "  DB::baz [src] — callees: DB::qux (lower bound: unresolved_calls)");
  assert.equal(
    lines[3],
    "  DB::Foo::run [src/Interpreters] — callers: DB::Main::go",
    "same file as bar: its tests are not repeated",
  );
  assert.equal(lines[4], "", "block ends with a newline");
  assert.equal(lines.length, 5, "the anchor with nothing to relate is not a line");
  assert.equal(
    renderGraphBlock([NEIGHBOURS[3]]).split("\n")[1],
    "  DB::Foo::run [src/Interpreters] — callers: DB::Main::go; tests: tests/queries/0_stateless/{0001_foo.sql, 0002_foo.sh}, tests/integration/test_foo/test.py",
  );
  assert.equal(renderGraphBlock([]), "");
  assert.equal(renderGraphBlock([NEIGHBOURS[1]]), "", "no related function: no block at all");
  assert.equal(renderGraphBlock(NEIGHBOURS, { maxAnchors: 2 }), "", "suppressed above the anchor cap");
  assert.equal(renderGraphBlock(NEIGHBOURS, { capped: true }), "", "server says more functions than it covered");
});

test("collapsePaths: one directory once, order kept", () => {
  assert.equal(collapsePaths(["a/b/x.sql", "a/b/y.sql", "c/z.py", "plain"]), "a/b/{x.sql, y.sql}, c/z.py, plain");
  assert.equal(collapsePaths(["a/x.sql"]), "a/x.sql");
});

test("stripGraphBlock: the block is strictly trailing and strips to the exact grep bytes", () => {
  const grep = "src/A.cpp:10:    foo();\nsrc/A.cpp:11:    [Related from loom dependency graph] in a comment\n";
  const out = grep + renderGraphBlock(NEIGHBOURS);
  assert.ok(out.startsWith(grep), "grep's bytes come first, untouched");
  assert.equal(stripGraphBlock(out), grep, "byte-identical once the block is stripped");
  assert.equal(stripGraphBlock(grep), grep, "no block: unchanged (a header inside a hit line is not a block)");
  assert.equal(stripGraphBlock(renderGraphBlock(NEIGHBOURS)), "", "block alone strips to nothing");
});
