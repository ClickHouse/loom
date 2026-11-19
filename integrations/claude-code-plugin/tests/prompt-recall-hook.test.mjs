import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hostEnv } from "./host-env.mjs";

const HOOK = fileURLToPath(new URL("../hooks/loom-prompt-recall.mjs", import.meta.url));
const PACK = {
  section_meta: { memories: { abstain: false, retrieval_confidence: 0.8 } },
  blocks: [
    { section: "memories", label: "recall.memories", rendered: "- use the unit suite", citations: ["memory:n/k"] },
  ],
};

async function fixture(t, response, mode = "first") {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "loom-recall-hook-"));
  const requests = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      requests.push({ url: req.url, body: JSON.parse(body) });
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify(response));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(tmp, { recursive: true, force: true });
  });
  const env = hostEnv({
    PATH: process.env.PATH,
    HOME: tmp,
    TMPDIR: tmp,
    LOOM_BASE_URL: `http://127.0.0.1:${server.address().port}`,
    LOOM_API_KEY: "test-token",
    LOOM_ORG: "o",
    LOOM_NAMESPACE: "n",
    LOOM_PROMPT_RECALL: mode,
    LOOM_HOOK_DEBUG: "on",
  });
  const run = (prompt) =>
    new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [HOOK, "UserPromptSubmit"], { cwd: tmp, env });
      let out = "";
      let err = "";
      child.on("error", reject);
      child.stdout.on("data", (chunk) => (out += chunk));
      child.stderr.on("data", (chunk) => (err += chunk));
      child.on("close", (code) => resolve({ code, out, err }));
      child.stdin.end(JSON.stringify({ cwd: tmp, session_id: "session", prompt }));
    });
  return { requests, run };
}

for (const [name, response] of [
  ["an abstaining answer", { blocks: [], section_meta: { memories: { abstain: true } } }],
  ["a low-confidence answer", { ...PACK, section_meta: { memories: { retrieval_confidence: 0.2 } } }],
]) {
  test(`first-prompt recall spends one request after ${name}`, async (t) => {
    const { requests, run } = await fixture(t, response);
    for (const prompt of ["why did this test fail?", "which test should I run?"]) {
      assert.deepEqual(await run(prompt), { code: 0, out: "", err: "" });
    }
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "/v1/context.pack");
  });
}

test("first-prompt recall emits an accepted answer once", async (t) => {
  const { requests, run } = await fixture(t, PACK);
  const first = await run("why did this test fail?");
  assert.equal(first.code, 0);
  assert.equal(first.err, "");
  assert.match(JSON.parse(first.out).hookSpecificOutput.additionalContext, /use the unit suite/);
  assert.deepEqual(await run("which test should I run?"), { code: 0, out: "", err: "" });
  assert.equal(requests.length, 1);
});

test("unqualified prompts leave the first recall available", async (t) => {
  const { requests, run } = await fixture(t, PACK);
  for (const prompt of [
    "yes",
    "/loom:loom-status",
    "Use only the supplied excerpt. What does it return?",
    "Review my uncommitted changes in parser.py.",
  ]) {
    assert.deepEqual(await run(prompt), { code: 0, out: "", err: "" });
  }
  assert.equal(requests.length, 0);
  const first = await run("why did this test fail?");
  assert.equal(first.code, 0);
  assert.match(first.out, /use the unit suite/);
  assert.equal(requests.length, 1);
});

test("recall on attempts each qualifying prompt after an empty answer", async (t) => {
  const { requests, run } = await fixture(t, { blocks: [] }, "on");
  for (const prompt of ["why did this test fail?", "which test should I run?"]) {
    assert.deepEqual(await run(prompt), { code: 0, out: "", err: "" });
  }
  assert.equal(requests.length, 2);
});

for (const prompt of [
  "Where is Parser.parse_record defined, and what arguments does it take?",
  "Which callers of Parser.parse_record can you establish from this checkout?",
  "What code could be affected if I change how Parser.parse_record validates names?",
]) {
  test(`default recall skips a named code-only question: ${prompt}`, async (t) => {
    const response = { blocks: [] };
    const { requests, run } = await fixture(t, response);
    assert.deepEqual(await run(prompt), { code: 0, out: "", err: "" });
    assert.equal(requests.length, 0);
    Object.assign(response, PACK);
    assert.match((await run("What did we decide about record prefixes?")).out, /use the unit suite/);
    assert.equal(requests.length, 1, "skipping code leaves the first recall attempt available");
  });
}

for (const memory of [
  "Include our saved decision.",
  "Include our memory.",
  "Include our conventions.",
  "Include our lessons.",
  "Recall our rationale.",
  "What did we decide about record prefixes?",
]) {
  test(`default recall keeps a mixed code question with ${memory}`, async (t) => {
    const { requests, run } = await fixture(t, PACK);
    const result = await run(`Where is Parser.parse_record defined? ${memory}`);
    assert.equal(result.code, 0);
    assert.match(result.out, /use the unit suite/);
    assert.equal(requests.length, 1);
  });
}

test("explicit on still recalls for each named code question", async (t) => {
  const { requests, run } = await fixture(t, { blocks: [] }, "on");
  for (const prompt of ["Where is Parser.parse_record defined?", "Who calls Parser.parse_record?"])
    assert.deepEqual(await run(prompt), { code: 0, out: "", err: "" });
  assert.equal(requests.length, 2);
});

for (const mode of ["first", "on"]) {
  for (const prompt of [
    "Review my uncommitted changes in parser.py. Cite the saved decision about validation.",
    "Find the exact error string invalid record locally. What did we decide about that error?",
  ]) {
    test(`${mode} recall keeps an explicit memory request alongside local code: ${prompt}`, async (t) => {
      const { requests, run } = await fixture(t, PACK, mode);
      const result = await run(prompt);
      assert.match(result.out, /use the unit suite/);
      assert.equal(requests.length, 1);
    });
  }
  test(`${mode} recall respects supplied-only evidence even when it mentions decisions`, async (t) => {
    const { requests, run } = await fixture(t, PACK, mode);
    assert.deepEqual(await run("Use only the supplied excerpt. It describes our saved decision."), {
      code: 0,
      out: "",
      err: "",
    });
    assert.equal(requests.length, 0);
  });
}
