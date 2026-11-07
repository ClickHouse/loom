// The suite never writes the block-id key (lib/prompt-recall.mjs, $HOME/.loom/recall-key)
// into the home of whoever runs it: every test file imports temp-home.mjs first.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const TESTS = path.dirname(fileURLToPath(import.meta.url));

test("every test file imports temp-home.mjs before anything else", () => {
  for (const n of fs.readdirSync(TESTS).filter((x) => x.endsWith(".test.mjs"))) {
    const first = fs
      .readFileSync(path.join(TESTS, n), "utf8")
      .split(/\r?\n/) // a Windows checkout has CRLF
      .find((l) => l.startsWith("import "));
    assert.equal(first, 'import "./temp-home.mjs";', n);
  }
});
