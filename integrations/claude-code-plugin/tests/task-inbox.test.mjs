// The session-start task inbox line — one line when work waits, silence otherwise.
import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CACHE_TTL_MS,
  cacheIsFresh,
  countsBody,
  formatAge,
  renderTaskLine,
  summarizeCounts,
} from "../lib/task-inbox.mjs";

const NOW = Date.parse("2026-09-20T12:00:00Z");
const row = (inbox, status, n, oldest) => ({ inbox, status, n, oldest });

test("countsBody: the caller's own scope, nothing else", () => {
  assert.deepEqual(countsBody({ org: "acme", namespace: "private", agent: "claude-code" }), {
    org: "acme",
    namespace: "private",
    agent: "claude-code",
  });
  assert.equal(countsBody({ org: "acme", namespace: "private" }).agent, "-");
});

test("summarizeCounts: triage + todo across inboxes, the busiest inbox, the oldest waiting item", () => {
  const s = summarizeCounts(
    {
      counts: [
        row("private-bot", "triage", 2, "2026-09-20 11:20:00"),
        row("private-bot", "todo", 3, "2026-09-20 11:50:00"),
        row("private-bot", "done", 40, "2026-09-01 00:00:00"), // finished work never counts
        row("review", "todo", 1, "2026-09-20T11:58:00Z"),
        row("review", "in_progress", 1, "2026-09-20 11:00:00"),
      ],
    },
    NOW,
  );
  assert.equal(s.triage, 2);
  assert.equal(s.todo, 4);
  assert.equal(s.inbox, "private-bot");
  assert.equal(s.inboxes, 2);
  assert.equal(s.oldestMs, 40 * 60 * 1000);
});

test("renderTaskLine: the one line, in the documented shape", () => {
  const s = summarizeCounts(
    {
      counts: [
        row("private-bot", "triage", 2, "2026-09-20 11:20:00"),
        row("private-bot", "todo", 3, "2026-09-20 11:50:00"),
      ],
    },
    NOW,
  );
  assert.equal(
    renderTaskLine(s),
    "Loom task inbox: 2 in triage, 3 to do in private-bot (oldest 40 min) — pull with the loom `tasks` toolset (task.pull)",
  );
  // one part when the other is zero; several inboxes named by the busiest
  assert.equal(
    renderTaskLine(summarizeCounts({ counts: [row("bot", "todo", 1, null), row("other", "todo", 1, null)] }, NOW)),
    "Loom task inbox: 2 to do in bot +1 more — pull with the loom `tasks` toolset (task.pull)",
  );
  assert.equal(renderTaskLine(summarizeCounts({ counts: [row("bot", "triage", 5, null)] }, NOW)).split("\n").length, 1);
});

test("renderTaskLine: silence on an empty board, an error, a 404, junk", () => {
  assert.equal(renderTaskLine(summarizeCounts({ counts: [] }, NOW)), "");
  assert.equal(renderTaskLine(summarizeCounts({ counts: [row("bot", "done", 9, null)] }, NOW)), "");
  assert.equal(renderTaskLine(summarizeCounts(null, NOW)), ""); // postBounded → null on 404 / 403 / timeout
  assert.equal(renderTaskLine(summarizeCounts({ detail: "nope" }, NOW)), "");
  assert.equal(renderTaskLine(summarizeCounts({ counts: [null, { status: "todo", n: "x" }] }, NOW)), "");
});

test("formatAge: minutes, hours, days; unknown is empty", () => {
  assert.equal(formatAge(40 * 60 * 1000), "40 min");
  assert.equal(formatAge(20 * 1000), "1 min");
  assert.equal(formatAge(3 * 3600 * 1000), "3 h");
  assert.equal(formatAge(3 * 86400 * 1000), "3 d");
  assert.equal(formatAge(null), "");
  assert.equal(formatAge(undefined), "");
});

test("cacheIsFresh: a minute per scope, and only a real entry", () => {
  assert.equal(CACHE_TTL_MS, 60_000);
  assert.equal(cacheIsFresh({ ts: NOW - 30_000, line: "" }, NOW), true); // an empty board is cached too
  assert.equal(cacheIsFresh({ ts: NOW - 61_000, line: "x" }, NOW), false);
  assert.equal(cacheIsFresh({ ts: NOW }, NOW), false);
  assert.equal(cacheIsFresh(null, NOW), false);
});
