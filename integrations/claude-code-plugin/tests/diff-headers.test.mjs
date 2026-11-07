// diffHeaders: what the code-plane hooks send of a diff is the file headers and
// the hunk ranges, with only the function name the server reads from a hunk's
// context. No changed line is sent. tests/unit/test_hook_diff_headers.py checks
// the result against the server's own parser.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { diffHeaders, hunkFunctionName } from "../lib/loom-client.mjs";

const PEM_LINE = "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun";

const DIFF = [
  "diff --git a/src/k.cpp b/src/k.cpp",
  "index 1111111..2222222 100644",
  "--- a/src/k.cpp",
  "+++ b/src/k.cpp",
  "@@ -10 +10 @@ bool PrivateKey::verify(const std::string & data)",
  '-  const char* api_key = "old-s3cr3t";',
  '+  const char* api_key = "new-s3cr3t";',
  "@@ -40,0 +41,2 @@ DB_PASSWORD=hunter2",
  `+${PEM_LINE}`,
  "+  password: |",
  "diff --git a/tls.pem b/tls.pem",
  "--- a/tls.pem",
  "+++ b/tls.pem",
  `@@ -2 +2 @@ ${PEM_LINE}`,
  `-${PEM_LINE}`,
  `+${PEM_LINE.split("").reverse().join("")}`,
  "",
].join("\n");

test("file headers and hunk ranges are kept; no changed line is", () => {
  const out = diffHeaders(DIFF, 200_000);
  for (const line of [
    "diff --git a/src/k.cpp b/src/k.cpp",
    "+++ b/src/k.cpp",
    "@@ -10 +10 @@ PrivateKey::verify(",
    "@@ -40,0 +41,2 @@",
    "+++ b/tls.pem",
    "@@ -2 +2 @@",
  ]) {
    assert.ok(out.split("\n").includes(line), `${line} kept:\n${out}`);
  }
  for (const leak of ["s3cr3t", "hunter2", "MIIE", "password", "std::string"]) {
    assert.ok(!out.includes(leak), `${leak} sent:\n${out}`);
  }
});

test("only the function name the server reads is kept from a hunk's context", () => {
  const cases = [
    ["bool PrivateKey::verify(const std::string & data)", "PrivateKey::verify"],
    ["template <class T> void Foo<T>::bar(int x)", "Foo::bar"],
    ["pub fn parse<'a>(input: &'a str)", "parse"],
    ['extern "C" int init(void)', "init"],
    ['TOKEN="ghp_abcdefghijklmnopqrstuvwx" + f(x)', "f"],
    ["Password: S3cretValue (staging)", ""],
    ["Deploy with token ghp_0123456789abcdefghijABCDEFGHIJ0123 (rotate monthly)", ""],
    ["key AKIA" + "IOSFODNN7EXAMPLE (prod)", ""],
    ["const handler = function handler(req)", "handler"],
    ["public: Foo(int x)", "Foo"],
    ["  private: static Bar* make(int)", "make"],
    ["password = getpass(prompt)", ""],
    ["    if (ready) {", ""],
    ["DB_PASSWORD=hunter2", ""],
  ];
  for (const [ctx, name] of cases) {
    assert.equal(hunkFunctionName(ctx), name, ctx);
    const diff = ["diff --git a/x.cpp b/x.cpp", "+++ b/x.cpp", `@@ -1 +1 @@ ${ctx}`, "+x", ""].join("\n");
    assert.ok(
      diffHeaders(diff, 200_000)
        .split("\n")
        .includes(name ? `@@ -1 +1 @@ ${name}(` : "@@ -1 +1 @@"),
      ctx,
    );
  }
});

test("a file that is not code sends ranges and no name", () => {
  const diff = [
    "diff --git a/RUNBOOK.md b/RUNBOOK.md",
    "+++ b/RUNBOOK.md",
    "@@ -12 +12 @@ Staging password hunter2 (rotate monthly)",
    "+x",
    "",
  ].join("\n");
  assert.deepEqual(diffHeaders(diff, 200_000).split("\n"), [
    "diff --git a/RUNBOOK.md b/RUNBOOK.md",
    "+++ b/RUNBOOK.md",
    "@@ -12 +12 @@",
  ]);
});

test("a header-shaped text after a line end inside a changed line is not taken for a header", () => {
  const diff = [
    "diff --git a/p.cpp b/p.cpp",
    "+++ b/p.cpp",
    "@@ -1 +1,2 @@ void f(int)",
    "+x\fdiff --git a/s b/s",
    "+\f+++ b/secret-path password=hunter2",
    "",
  ].join("\n");
  const out = diffHeaders(diff, 200_000);
  assert.ok(!out.includes("hunter2") && !out.includes("secret-path"), out);
  assert.deepEqual(out.split("\n"), ["diff --git a/p.cpp b/p.cpp", "+++ b/p.cpp", "@@ -1 +1,2 @@ f("]);
});

test("the hunks of a file whose header is dropped are not filed under the file before it", () => {
  const diff = [
    "diff --git a/a.cpp b/a.cpp",
    "+++ b/a.cpp",
    "@@ -1 +1 @@ void f(int)",
    "+x",
    "diff --git a/odd\u2028name.cpp b/odd\u2028name.cpp",
    "+++ b/odd\u2028name.cpp",
    "@@ -5 +5 @@ void g(int)",
    "+y",
    "diff --git a/c.py b/c.py",
    "+++ b/c.py",
    "@@ -9 +9 @@ def h(x):",
    "",
  ].join("\n");
  assert.deepEqual(diffHeaders(diff, 200_000).split("\n"), [
    "diff --git a/a.cpp b/a.cpp",
    "+++ b/a.cpp",
    "@@ -1 +1 @@ f(",
    "diff --git a/c.py b/c.py",
    "+++ b/c.py",
    "@@ -9 +9 @@ h(",
  ]);
});

test("a line end in a file's first hunk context cuts that header, not the file", () => {
  const diff = [
    "diff --git a/a.py b/a.py",
    "--- a/a.py",
    "+++ b/a.py",
    "@@ -3 +3 @@ def f(x):  # a\u2028b",
    "+y",
    "@@ -9 +9 @@ def g():",
    "+z",
    "",
  ].join("\n");
  assert.deepEqual(diffHeaders(diff, 200_000).split("\n"), [
    "diff --git a/a.py b/a.py",
    "--- a/a.py",
    "+++ b/a.py",
    "@@ -3 +3 @@ f(",
    "@@ -9 +9 @@ g(",
  ]);
});

test("an added line that starts with ++ is not taken for a new file", () => {
  const diff = [
    "diff --git a/a.cpp b/a.cpp",
    "+++ b/a.cpp",
    "@@ -1 +1,2 @@ void f(int)",
    "+++ x",
    "+y",
    "@@ -9 +10 @@ void g(int)",
    "+z",
    "",
  ].join("\n");
  assert.deepEqual(diffHeaders(diff, 200_000).split("\n"), [
    "diff --git a/a.cpp b/a.cpp",
    "+++ b/a.cpp",
    "@@ -1 +1,2 @@ f(",
    "@@ -9 +10 @@ g(",
  ]);
});

test("only diff --git, --- and +++ lines are kept from a file's header", () => {
  const diff = [
    "diff --git a/x b/x",
    "index 1111111..2222222 100644",
    "--- a/x",
    "+++ b/x",
    "Submodule sub 1111111..2222222:",
    "  > a commit subject naming a password",
    "@@ -1 +1 @@",
    "",
  ].join("\n");
  assert.deepEqual(diffHeaders(diff, 200_000).split("\n"), ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -1 +1 @@"]);
});

test("a cut never splits a line", () => {
  const diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n";
  for (let max = 0; max < diff.length + 2; max++) {
    const out = diffHeaders(diff, max);
    assert.ok(out.length <= max, `${max}: ${out.length}`);
    for (const line of out.split("\n").filter(Boolean)) assert.ok(diff.split("\n").includes(line), `${max}: ${line}`);
    // a file header with no hunk after it would make the server seed the whole file
    assert.ok(out === "" || out.split("\n").at(-1).startsWith("@@"), `${max}: ends in ${out.split("\n").at(-1)}`);
  }
});

test("text before the first file header is not sent", () => {
  const out = diffHeaders("password=hunter2\n\u001b[1mdiff --git a/x b/x\u001b[m\n", 200_000);
  assert.equal(out, "");
});

test("the result never exceeds the bound", () => {
  const hunks = Array.from({ length: 20_000 }, (_, i) => `@@ -${i} +${i} @@ void f${i}(int x)\n+x;`).join("\n");
  const big = `diff --git a/a b/a\n+++ b/a\n${hunks}\n`;
  assert.ok(big.length > 200_000);
  assert.ok(diffHeaders(big, 200_000).length <= 200_000);
});
