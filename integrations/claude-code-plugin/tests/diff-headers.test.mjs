// diffHeaders: what the code-plane hooks send of a diff is the file headers and
// the hunk ranges, with a hunk's function context only up to its first `(`.
// No changed line is sent.
import { test } from "node:test";
import assert from "node:assert/strict";
import { diffHeaders } from "../lib/loom-client.mjs";

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
    "@@ -10 +10 @@ bool PrivateKey::verify(",
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

test("the server reads the same ranges and function names as from the whole diff", () => {
  // The fields _parse_unified_diff_full takes: the +++ path, the new-side range,
  // and the context before its first `(`.
  const parse = (text) =>
    text
      .split("\n")
      .filter((l) => l.startsWith("+++ ") || l.startsWith("@@"))
      .map((l) => {
        const m = /^@@ -\d+(?:,\d+)? \+(\d+(?:,\d+)?) @@(.*)$/.exec(l);
        return m ? `${m[1]} ${m[2].includes("(") ? m[2].split("(")[0].trim() : ""}` : l;
      });
  assert.deepEqual(parse(diffHeaders(DIFF, 200_000)), parse(DIFF));
});

test("a context whose head is not a function head is dropped, ranges kept", () => {
  for (const ctx of [
    'TOKEN="ghp_abcdefghijklmnopqrstuvwx" + f(x)',
    "password = getpass(prompt)",
    "api_key: str = load(path)",
  ]) {
    const diff = ["diff --git a/x b/x", "+++ b/x", `@@ -1 +1 @@ ${ctx}`, "+x", ""].join("\n");
    assert.ok(diffHeaders(diff, 200_000).split("\n").includes("@@ -1 +1 @@"), ctx);
  }
  const named = ["diff --git a/x b/x", "+++ b/x", "@@ -1 +1 @@ std::vector<int> Foo<T>::bar(int x)", ""].join("\n");
  assert.ok(diffHeaders(named, 200_000).includes("@@ -1 +1 @@ std::vector<int> Foo<T>::bar("), named);
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
