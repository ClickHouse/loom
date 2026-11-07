// The suite never writes the block-id key (lib/prompt-recall.mjs, $HOME/.loom/recall-key)
// into the home of whoever runs it: every test file imports temp-home.mjs first.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const TESTS = path.dirname(fileURLToPath(import.meta.url));
// The test files that build recalled-data blocks in their own process.
const BUILDERS = [
  "code-shunt.test.mjs",
  "collections-line.test.mjs",
  "file-context.test.mjs",
  "prompt-recall.test.mjs",
  "session-brief.test.mjs",
  "session-start.test.mjs",
];

test("every test file imports temp-home.mjs before anything else", () => {
  for (const n of fs.readdirSync(TESTS).filter((x) => x.endsWith(".test.mjs"))) {
    const first = fs
      .readFileSync(path.join(TESTS, n), "utf8")
      .split("\n")
      .find((l) => l.startsWith("import "));
    assert.equal(first, 'import "./temp-home.mjs";', n);
  }
});

test("the tests that build blocks write nothing under the home they were started with", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-real-home-"));
  try {
    const env = { ...process.env, HOME: home };
    delete env.NODE_TEST_CONTEXT; // the child reports as a runner of its own
    const run = spawnSync(process.execPath, ["--test", ...BUILDERS.map((n) => path.join(TESTS, n))], {
      env,
      encoding: "utf8",
      timeout: 300_000,
    });
    assert.equal(run.status, 0, run.stdout.slice(-2000));
    assert.match(run.stdout, /# pass [1-9]\d*/);
    assert.deepEqual(fs.readdirSync(home), [], "the runner's home is untouched");
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});
