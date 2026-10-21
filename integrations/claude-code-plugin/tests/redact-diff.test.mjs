// redactDiff: a diff the code-plane hooks send keeps every line the server parses
// (`+++ ` paths and `@@` hunk headers) and stays within the server's bound.
import { test } from "node:test";
import assert from "node:assert/strict";
import { redactDiff } from "../lib/loom-client.mjs";

const DIFF = [
  "diff --git a/app.env b/app.env",
  "index 1111111..2222222 100644",
  "--- a/app.env",
  "+++ b/app.env",
  "@@ -1,0 +2 @@",
  "+API_KEY=",
  "@@ -3,0 +4 @@ def f():",
  '+password = "hunter2hunter2"',
  "@@ -9 +10 @@",
  "--- old comment password=abc123",
  "+++ new comment",
  "",
].join("\n");

test("hunk headers and paths survive a secret at the end of a hunk", () => {
  const out = redactDiff(DIFF, 200_000, "");
  for (const line of ["+++ b/app.env", "@@ -1,0 +2 @@", "@@ -3,0 +4 @@ def f():", "@@ -9 +10 @@"]) {
    assert.ok(out.split("\n").includes(line), `${line} kept:\n${out}`);
  }
  assert.ok(!out.includes("hunter2hunter2"), out);
  assert.ok(out.includes('+password = "[redacted]"'), out);
});

test("a removed line that starts like a file header is still redacted", () => {
  const out = redactDiff(DIFF, 200_000, "");
  assert.ok(!out.includes("abc123"), out);
  assert.ok(out.includes("--- old comment password=[redacted]"), out);
});

test("an unterminated private key is redacted to the end of its hunk only", () => {
  const diff = [
    "diff --git a/tls.py b/tls.py",
    "--- a/tls.py",
    "+++ b/tls.py",
    "@@ -1,0 +1 @@",
    '+MARKER = "-----BEGIN RSA ' + 'PRIVATE KEY-----"',
    "diff --git a/other.py b/other.py",
    "--- a/other.py",
    "+++ b/other.py",
    "@@ -5 +5 @@",
    "+x = 1",
  ].join("\n");
  const out = redactDiff(diff, 200_000, "");
  assert.ok(out.includes("[redacted private key]"), out);
  assert.ok(out.includes("+++ b/other.py") && out.includes("@@ -5 +5 @@") && out.includes("+x = 1"), out);
});

test("the function context git appends to a hunk header is redacted, the range kept", () => {
  const diff = [
    "diff --git a/.env b/.env",
    "--- a/.env",
    "+++ b/.env",
    "@@ -2 +2 @@ DB_PASSWORD=hunter2",
    "-API_KEY=old",
    "+API_KEY=new",
    "",
  ].join("\n");
  const out = redactDiff(diff, 200_000, "");
  assert.ok(!out.includes("hunter2"), out);
  assert.ok(out.includes("@@ -2 +2 @@ DB_PASSWORD=[redacted]"), out);
  const named = redactDiff(diff.replace("DB_PASSWORD=hunter2", "def f(x):"), 200_000, "");
  assert.ok(named.includes("@@ -2 +2 @@ def f(x):"), named);
});

test("text that is not in git's diff format is redacted as a body", () => {
  const out = redactDiff("\u001b[1mdiff --git a/x b/x\u001b[m\n+password=hunter2\n", 200_000, "");
  assert.ok(!out.includes("hunter2"), out);
  const fake = redactDiff("@@ api_key=hunter2 @@\n", 200_000, "");
  assert.ok(!fake.includes("hunter2"), fake);
});

test("a C++ scope operator in a hunk header's context is not read as an assignment", () => {
  for (const ctx of ["bool PrivateKey::verify(int x)", "AccessToken::AccessToken(const String & s)"]) {
    const diff = ["diff --git a/k.cpp b/k.cpp", "+++ b/k.cpp", `@@ -1 +1 @@ ${ctx}`, "+x();", ""].join("\n");
    assert.ok(redactDiff(diff, 200_000, "").includes(`@@ -1 +1 @@ ${ctx}`), ctx);
  }
});

test("the result never exceeds the bound, with or without redactions", () => {
  const body = "+" + "y".repeat(99) + "\n";
  const big = "diff --git a/a.txt b/a.txt\n+++ b/a.txt\n@@ -1,0 +1,3000 @@\n" + body.repeat(2_200);
  assert.ok(big.length > 216_384);
  assert.equal(redactDiff(big, 200_000, "").length, 200_000);
  const withSecret = big.replace(body, "+token=" + "z".repeat(40) + "\n");
  assert.ok(redactDiff(withSecret, 200_000, "").length <= 200_000);
  assert.ok(redactDiff("x".repeat(210_000), 200_000, "").length <= 200_000);
});
