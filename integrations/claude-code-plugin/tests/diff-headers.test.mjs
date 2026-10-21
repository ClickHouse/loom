// diffHeaders: what the code-plane hooks send of a diff is the file headers and
// the hunk ranges, with only the function name the server reads from a hunk's
// context. No changed line is sent. tests/unit/test_hook_diff_headers.py checks
// the result against the server's own parser.
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
    ['TOKEN="ghp_abcdefghijklmnopqrstuvwx" + f(x)', ""],
    ["Password: S3cretValue (staging)", ""],
    ["password = getpass(prompt)", ""],
    ["    if (ready) {", ""],
    ["DB_PASSWORD=hunter2", ""],
  ];
  for (const [ctx, name] of cases) {
    assert.equal(hunkFunctionName(ctx), name, ctx);
    const diff = ["diff --git a/x b/x", "+++ b/x", `@@ -1 +1 @@ ${ctx}`, "+x", ""].join("\n");
    assert.ok(
      diffHeaders(diff, 200_000)
        .split("\n")
        .includes(name ? `@@ -1 +1 @@ ${name}(` : "@@ -1 +1 @@"),
      ctx,
    );
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
