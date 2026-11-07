// The turn journal — the hooks' own record of prompts and
// replies, for clients that hand them no parseable transcript.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  JOURNAL_MAX_CHARS,
  appendTurn,
  firstUser,
  journalCursorPath,
  journalPath,
  journalRow,
  journalTurns,
  lastAssistant,
  parseJournal,
  readJournal,
  readJournalCursor,
  rowsSince,
} from "../lib/turn-journal.mjs";

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "loom-journal-test-"));
}

test("journalRow: trims, bounds, folds unknown roles onto user, drops empties", () => {
  assert.equal(journalRow("user", "   "), null);
  assert.equal(journalRow("assistant", "ok", "2026-09-16T00:00:00Z").role, "assistant");
  assert.equal(journalRow("tool", "x", "t").role, "user");
  assert.equal(journalRow("user", "a".repeat(JOURNAL_MAX_CHARS + 50), "t").content.length, JOURNAL_MAX_CHARS);
});

test("parseJournal: keeps well-formed rows with their line index, skips the rest", () => {
  const text = [
    JSON.stringify({ role: "user", content: "hi", at: "t1" }),
    "not json",
    JSON.stringify({ role: "system", content: "no" }),
    "",
    JSON.stringify({ role: "assistant", content: "hello", at: "t2" }),
  ].join("\n");
  const rows = parseJournal(text);
  assert.deepEqual(
    rows.map((r) => [r.role, r.content, r.i]),
    [
      ["user", "hi", 0],
      ["assistant", "hello", 4],
    ],
  );
  assert.deepEqual(
    rowsSince(rows, 1).map((r) => r.i),
    [4],
  );
  assert.deepEqual(rowsSince(rows, 0).length, 2);
  assert.equal(firstUser(rows), "hi");
  assert.equal(lastAssistant(rows), "hello");
  assert.deepEqual(journalTurns(rows)[1], { role: "assistant", content: "hello", timestamp: "t2" });
});

test("appendTurn / readJournal round-trip in a session file; paths hash the session id", () => {
  const dir = tmp();
  assert.ok(appendTurn("sess/1", "user", "fix the build", { dir, at: "t1" }));
  assert.ok(appendTurn("sess/1", "assistant", "fixed", { dir, at: "t2" }));
  assert.equal(appendTurn("sess/1", "assistant", "", { dir }), false);
  assert.equal(appendTurn("", "user", "x", { dir }), false);
  const rows = readJournal("sess/1", { dir });
  assert.deepEqual(
    rows.map((r) => r.content),
    ["fix the build", "fixed"],
  );
  assert.match(path.basename(journalPath("sess/1", dir)), /^loom-turns-[0-9a-f]{12}\.jsonl$/);
  assert.match(path.basename(journalCursorPath("sess/1", dir)), /^loom-turns-[0-9a-f]{12}\.cursor$/);
  assert.equal(readJournalCursor("sess/1", { dir }), 0);
  fs.writeFileSync(journalCursorPath("sess/1", dir), "2\n");
  assert.equal(readJournalCursor("sess/1", { dir }), 2);
  assert.deepEqual(readJournal("other", { dir }), []);
});

test("journalRow redacts before it clips, so a secret on the cut is still found", () => {
  const secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8";
  const row = journalRow("user", "x ".repeat((JOURNAL_MAX_CHARS - 10) / 2) + secret);
  assert.ok(!row.content.includes(secret.slice(4, 10)), row.content.slice(-40));
});
