import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseLines } from "../lib/transcript.mjs";
import { extractTurns, recordBody } from "../lib/usage.mjs";
import { extractTurns as extractCodexTurns } from "../../codex-plugin/lib/usage.mjs";

const LINE = (id, extra = {}) =>
  JSON.stringify({
    type: "assistant",
    uuid: `u-${id}`,
    timestamp: "2026-09-11T17:00:00.000Z",
    isSidechain: false,
    message: {
      id,
      model: "claude-fable-5-1",
      usage: {
        input_tokens: 2,
        cache_creation_input_tokens: 16146,
        cache_read_input_tokens: 29592,
        output_tokens: 292,
        cache_creation: { ephemeral_1h_input_tokens: 16146, ephemeral_5m_input_tokens: 0 },
      },
    },
    ...extra,
  });

test("extractTurns dedupes by message id, skips sidechains and non-assistant, splits cache writes by TTL", () => {
  const objs = parseLines(
    [
      LINE("m1"),
      LINE("m1"), // streamed twice
      LINE("m2", { isSidechain: true }), // subagent: skipped
      JSON.stringify({ type: "user", message: { content: "hi" } }),
      JSON.stringify({ type: "assistant", message: { id: "m3", model: "claude-opus-5" } }), // no usage: not a row
      LINE("m4"),
    ].join("\n"),
  );
  const { turns, next } = extractTurns(objs);
  assert.deepEqual(
    turns.map((t) => t.message_id),
    ["m1", "m4"],
  );
  assert.equal(turns[0].cache_creation_1h_tokens, 16146);
  assert.equal(turns[0].cache_creation_5m_tokens, 0);
  assert.equal(turns[0].cache_read_tokens, 29592);
  assert.equal(turns[0].output_tokens, 292);
  assert.equal(turns[0].turn_at, "2026-09-11T17:00:00.000Z");
  assert.ok(next.has("m1") && next.has("m4") && !next.has("m2"));
  const again = extractTurns(objs, next);
  assert.equal(again.turns.length, 0, "already posted turns are not re-sent");
});

test("missing TTL split treats the whole write as 5-minute", () => {
  const o = {
    type: "assistant",
    message: {
      id: "x",
      model: "claude-sonnet-5",
      usage: { input_tokens: 10, cache_creation_input_tokens: 500, cache_read_input_tokens: 0, output_tokens: 5 },
    },
  };
  const { turns } = extractTurns([o]);
  assert.equal(turns[0].cache_creation_5m_tokens, 500);
  assert.equal(turns[0].cache_creation_1h_tokens, 0);
});

test("recordBody carries the identity tuple and the consumer", () => {
  const b = recordBody({ org: "o", namespace: "n", agent: "claude" }, "s1", [{ message_id: "m" }]);
  assert.equal(b.org, "o");
  assert.equal(b.session_id, "s1");
  assert.equal(b.agent, "claude");
  assert.equal(b.user_id, "-");
  assert.equal(b.consumer, "claude-code");
  assert.equal(b.turns.length, 1);
});

for (const usage of [
  {},
  { input_tokens: 9 },
  { output_tokens: 2 },
  { input_tokens: -1, output_tokens: 2 },
  { input_tokens: 1.5, output_tokens: 2 },
  { input_tokens: "9", output_tokens: 2 },
]) {
  test(`unmeasured token counters do not create a billing row: ${JSON.stringify(usage)}`, () => {
    const { turns, next } = extractTurns([{ type: "assistant", message: { id: "unknown", model: "model", usage } }]);
    assert.deepEqual(turns, []);
    assert.equal(next.has("unknown"), false);
  });
}

test("measured zero token counters remain a recorded row", () => {
  const { turns, next } = extractTurns([
    {
      type: "assistant",
      message: { id: "zero", model: "model", usage: { input_tokens: 0, output_tokens: 0 } },
    },
  ]);
  assert.equal(turns.length, 1);
  assert.equal(turns[0].input_tokens, 0);
  assert.equal(turns[0].output_tokens, 0);
  assert.equal(next.has("zero"), true);
});

test("an incomplete usage record does not hide a later measured record", () => {
  const message = { id: "eventual", model: "model" };
  const { turns } = extractTurns([
    { type: "assistant", message: { ...message, usage: {} } },
    { type: "assistant", message: { ...message, usage: { input_tokens: 9, output_tokens: 2 } } },
  ]);
  assert.equal(turns.length, 1);
  assert.equal(turns[0].input_tokens, 9);
  assert.equal(turns[0].output_tokens, 2);
});

test("Codex usage extraction preserves unknown counts, measured zero and message deduplication", () => {
  const row = (id, usage) => ({ type: "assistant", message: { id, model: "model", usage } });
  const { turns, next } = extractCodexTurns([
    row("unknown", { input_tokens: 9 }),
    row("eventual", {}),
    row("zero", { input_tokens: 0, output_tokens: 0 }),
    row("eventual", { input_tokens: 9, output_tokens: 2 }),
    row("eventual", { input_tokens: 9, output_tokens: 2 }),
  ]);
  assert.deepEqual(
    turns.map(({ message_id, input_tokens, output_tokens }) => [message_id, input_tokens, output_tokens]),
    [
      ["zero", 0, 0],
      ["eventual", 9, 2],
    ],
  );
  assert.equal(next.has("unknown"), false);
  assert.equal(next.size, 2);
  assert.deepEqual(extractCodexTurns([row("eventual", { input_tokens: 9, output_tokens: 2 })], next).turns, []);
});
