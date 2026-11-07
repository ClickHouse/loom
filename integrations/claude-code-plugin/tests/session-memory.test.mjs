import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { sessionMemoryBody, sessionToolEchoes, structuredPieces, transcriptTurns } from "../lib/session-memory.mjs";
import { journalTurns, parseJournal } from "../lib/turn-journal.mjs";

const scope = { org: "o", namespace: "coding", agent: "claude-code", project: "loom", tags: ["project:loom"] };
const objs = [
  { type: "user", timestamp: "2026-09-12T10:00:00Z", message: { content: "<command-name>/goal</command-name>" } },
  { type: "user", timestamp: "2026-09-12T10:00:01Z", message: { content: "Fix the flaky test in ingest." } },
  { type: "assistant", message: { content: [{ type: "tool_use", name: "Read" }] } },
  { type: "assistant", isSidechain: true, message: { content: [{ type: "text", text: "side" }] } },
  { type: "user", message: { content: [{ type: "tool_result", content: "…" }] } },
  {
    type: "assistant",
    timestamp: "2026-09-12T10:05:00Z",
    message: {
      content: [
        { type: "text", text: "The test raced the queue; " },
        { type: "text", text: "pinned the order." },
      ],
    },
  },
];

test("transcriptTurns keeps real prompts and replies only", () => {
  const t = transcriptTurns(objs);
  assert.deepEqual(
    t.map((x) => x.role),
    ["user", "assistant"],
  );
  assert.equal(t[0].content, "Fix the flaky test in ingest.");
  assert.equal(t[1].content, "The test raced the queue;\npinned the order.");
  assert.equal(t[1].timestamp, "2026-09-12T10:05:00Z");
});

test("sessionMemoryBody is a deferred set_from_messages for the session", () => {
  const b = sessionMemoryBody(scope, { session_id: "s1", cwd: "/repo" }, transcriptTurns(objs), {
    now: "2026-09-12T11:00:00Z",
  });
  assert.equal(b.org, "o");
  assert.equal(b.session_id, "s1");
  assert.equal(b.run_id, "cc-s1");
  assert.equal(b.defer, true);
  assert.equal(b.observation_date, "2026-09-12");
  assert.deepEqual(b.extra_tags, ["source:claude-code", "project:loom"]);
  assert.equal(b.messages.length, 2);
  assert.equal(b.messages[0].timestamp, "2026-09-12T10:00:01Z");
});

test("too little to remember is null; the tail is kept under the bound", () => {
  assert.equal(sessionMemoryBody(scope, { session_id: "s1" }, [{ role: "user", content: "hi" }]), null);
  assert.equal(
    sessionMemoryBody(scope, { session_id: "s1" }, [
      { role: "assistant", content: "a" },
      { role: "assistant", content: "b" },
    ]),
    null,
  );
  assert.equal(sessionMemoryBody(scope, {}, transcriptTurns(objs)), null);
  const turns = [];
  for (let i = 0; i < 300; i++)
    turns.push({ role: i % 2 ? "assistant" : "user", content: `turn ${i} ` + "x".repeat(900) });
  const b = sessionMemoryBody(scope, { session_id: "s1" }, turns, { maxTotalChars: 20000 });
  assert.ok(b.messages.length < 200);
  assert.ok(b.messages[b.messages.length - 1].content.startsWith("turn 299"));
  assert.ok(b.messages.reduce((n, m) => n + m.content.length, 0) <= 20000);
  const long = sessionMemoryBody(scope, { session_id: "s1" }, [
    { role: "user", content: "y".repeat(10000) },
    { role: "assistant", content: "ok" },
  ]);
  assert.ok(long.messages[0].content.length <= 6000 && long.messages[0].content.endsWith("[truncated]"));
});

test("transcriptTurns keeps the pointer to a pasted image and to an image a tool returned", () => {
  const png = { type: "image", source: { type: "base64", media_type: "image/png", data: "AAAA" } };
  const turns = transcriptTurns([
    { type: "user", timestamp: "t1", message: { content: [{ type: "text", text: "why is this red?" }, png] } },
    { type: "assistant", message: { content: [{ type: "tool_use", name: "Read" }] } },
    {
      type: "user",
      timestamp: "t2",
      message: { content: [{ type: "tool_result", tool_use_id: "x", content: [png] }] },
    },
    { type: "user", message: { content: [{ type: "tool_result", content: "plain text result" }] } },
    {
      type: "assistant",
      timestamp: "t3",
      message: { content: [{ type: "text", text: "The bar is a failing check." }] },
    },
  ]);
  assert.deepEqual(turns, [
    { role: "user", content: "why is this red?\n[image: image/png]", timestamp: "t1" },
    { role: "tool", content: "[image: 1 image result returned by a tool]", timestamp: "t2" },
    { role: "assistant", content: "The bar is a failing check.", timestamp: "t3" },
  ]);
  assert.ok(!JSON.stringify(turns).includes("AAAA"));
});

test("the session-memory body carries the keys the session recalled, deduped and capped", () => {
  const scope = { org: "o", namespace: "jdoe-ns", agent: "claude-code", tags: [] };
  const turns = [
    { role: "user", content: "what is my coffee type?", timestamp: "2026-09-20T08:00:00Z" },
    { role: "assistant", content: "Espresso.", timestamp: "2026-09-20T08:00:05Z" },
    { role: "user", content: "thanks", timestamp: "2026-09-20T08:00:09Z" },
  ];
  const b = sessionMemoryBody(scope, { session_id: "s1" }, turns, {
    recalledKeys: ["coffee_type", "coffee_type", "", "where_i_live"],
  });
  assert.deepEqual(b.recalled_keys, ["coffee_type", "where_i_live"]);
  const none = sessionMemoryBody(scope, { session_id: "s1" }, turns);
  assert.equal(none.recalled_keys, undefined, "nothing recalled: no field");
});

// What the session's own Loom tool calls carried rides to the
// extractor, so a fact restating it is dropped as an echo.
const loomSession = [
  { type: "user", message: { content: "save my merge preference" } },
  {
    type: "assistant",
    message: {
      content: [
        {
          type: "tool_use",
          id: "t1",
          name: "mcp__loom__loom_memory_set",
          input: { memory_key: "test/loomcheck/pref", value: "I prefer squash merges for small PRs" },
        },
      ],
    },
  },
  {
    type: "user",
    message: {
      content: [
        { type: "tool_result", tool_use_id: "t1", content: '{"memory_key":"test/loomcheck/pref","version":"1"}' },
      ],
    },
  },
  {
    type: "assistant",
    message: {
      content: [
        {
          type: "tool_use",
          id: "t2",
          name: "mcp__plugin_loom_loom__loom_search",
          input: { query: "what merge style do I prefer?" },
        },
        { type: "tool_use", id: "t3", name: "Bash", input: { command: "ls -la /some/where/long" } },
      ],
    },
  },
  {
    type: "user",
    message: {
      content: [
        {
          type: "tool_result",
          tool_use_id: "t2",
          content:
            '{"memory":{"results":[{"memory_key":"user_merge","content_excerpt":"User prefers rebase merges"},{"memory_key":"chunk:9:1"}]}}',
        },
        { type: "tool_result", tool_use_id: "t3", content: "total 0 drwxr-xr-x files" },
      ],
    },
  },
  { type: "attachment", attachment: { type: "hook_success", content: "# Loom memory primer — project: hooktest" } },
  { type: "assistant", message: { content: [{ type: "text", text: "Saved. You prefer squash merges." }] } },
];

test("sessionToolEchoes: loom tool args and results ride, asks and other tools do not", () => {
  const e = sessionToolEchoes(loomSession);
  assert.ok(e.values.includes("I prefer squash merges for small PRs"), "the written value");
  assert.ok(
    e.values.some((v) => v.includes("User prefers rebase merges")),
    "the search result",
  );
  assert.ok(!e.values.includes("what merge style do I prefer?"), "a query is the user's own words, not an echo");
  assert.ok(
    !e.values.some((v) => v.includes("drwxr-xr-x") || v.includes("/some/where")),
    "a non-loom tool is not an echo",
  );
  assert.deepEqual(e.keys.sort(), ["test/loomcheck/pref", "user_merge"], "keys named or served; chunks excluded");
});

test("sessionToolEchoes reads a Codex rollout's McpToolCall items", () => {
  const rollout = [
    { type: "session_meta", payload: { id: "01a0" } },
    {
      type: "event_msg",
      payload: {
        type: "item_completed",
        item: {
          type: "McpToolCall",
          server: "loom",
          tool: "loom_memory_set",
          arguments: { memory_key: "test/loomcheck-cx/pref", value: "I prefer squash merges for small PRs" },
          result: {
            content: [
              { type: "text", text: '{"memory_key": "test/loomcheck-cx/pref", "version": "1790324409380744712"}' },
            ],
          },
        },
      },
    },
    {
      type: "event_msg",
      payload: {
        type: "item_completed",
        item: {
          type: "McpToolCall",
          server: "loom",
          tool: "loom_inbox_list",
          arguments: { mine_everywhere: true },
          result:
            "{'content': [{'type': 'text', 'text': '{\"inboxes\": [{\"inbox\": \"loomcheck\", \"owner_namespace\": \"jdoe-ns\"}]}'}]}",
        },
      },
    },
    {
      type: "event_msg",
      payload: {
        type: "item_completed",
        item: {
          type: "McpToolCall",
          server: "github",
          tool: "search_issues",
          arguments: { q: "drwxr-xr-x something long" },
          result: "nothing here at all",
        },
      },
    },
    {
      type: "event_msg",
      payload: { type: "item_completed", item: { type: "CommandExecution", command: "ls -la /some/where" } },
    },
  ];
  const e = sessionToolEchoes(rollout);
  assert.ok(e.values.includes("I prefer squash merges for small PRs"), "the written value");
  assert.ok(
    e.values.some((v) => v.includes('"version": "1790324409380744712"')),
    "the structured result",
  );
  assert.ok(
    e.values.some((v) => v.includes("owner_namespace")),
    "a result stored as a string",
  );
  assert.ok(
    !e.values.some((v) => v.includes("drwxr-xr-x") || v.includes("nothing here")),
    "another server is not an echo",
  );
  assert.deepEqual(e.keys, ["test/loomcheck-cx/pref"]);
});

test("the primer attachment never becomes a turn the extractor reads", () => {
  const turns = transcriptTurns(loomSession);
  assert.ok(turns.every((t) => !t.content.includes("Loom memory primer")));
  assert.deepEqual(
    turns.map((t) => t.role),
    ["user", "assistant"],
  );
});

test("the session-memory body carries session_values and merges the echoed keys into recalled_keys", () => {
  const scope = { org: "o", namespace: "n", agent: "claude-code", tags: [] };
  const turns = transcriptTurns(loomSession);
  const b = sessionMemoryBody(scope, { session_id: "s1" }, turns, {
    recalledKeys: ["k0"],
    echoes: sessionToolEchoes(loomSession),
  });
  assert.ok(b.session_values.includes("I prefer squash merges for small PRs"));
  assert.deepEqual(b.recalled_keys.sort(), ["k0", "test/loomcheck/pref", "user_merge"]);
  const none = sessionMemoryBody(scope, { session_id: "s1" }, turns, {});
  assert.equal(none.session_values, undefined);
});

test("sessionToolEchoes is bounded", () => {
  const big = [];
  for (let i = 0; i < 200; i += 1) {
    big.push({
      type: "assistant",
      message: {
        content: [
          {
            type: "tool_use",
            id: `x${i}`,
            name: "mcp__loom__loom_memory_set",
            input: { memory_key: `k${i}`, value: `value number ${i} `.repeat(800) },
          },
        ],
      },
    });
  }
  const e = sessionToolEchoes(big);
  assert.ok(e.values.length <= 80 && e.values.join("").length <= 80000 && e.values.every((v) => v.length <= 8000));
  assert.ok(e.keys.length <= 200);
});

// A Codex result whose text is a pointer to a stored result is read
// from structuredContent, split into parseable rows when it is large.
test("sessionToolEchoes: a Codex pointer result is read from structuredContent", () => {
  const item = (result) => ({
    type: "event_msg",
    payload: {
      type: "item_completed",
      item: {
        type: "McpToolCall",
        server: "loom",
        tool: "loom_code_search",
        arguments: { query: "dup check" },
        result,
      },
    },
  });
  const small = sessionToolEchoes([
    item({
      content: [{ type: "text", text: "Result in structuredContent." }],
      structuredContent: {
        results: [{ path: "server/handlers/tracker_dupcheck.py", body: "The legs are tracker.similar's" }],
      },
    }),
  ]);
  assert.ok(!small.values.includes("Result in structuredContent."));
  assert.ok(small.values.some((v) => v.includes("tracker_dupcheck.py") && JSON.parse(v)));
  const rows = Array.from({ length: 40 }, (_, i) => ({ path: `f${i}.py`, body: "x".repeat(400) }));
  const big = sessionToolEchoes([
    item({
      content: [{ type: "text", text: "Result in structuredContent." }],
      structuredContent: { results: rows, abstain: false },
    }),
  ]);
  assert.ok(big.values.length >= 40 && big.values.every((v) => JSON.parse(v)));
});

test("structuredPieces: whole when it fits, one piece per row when not", () => {
  assert.deepEqual(structuredPieces(null), []);
  assert.equal(structuredPieces({ a: 1 }).length, 1);
  const big = structuredPieces({ rows: Array.from({ length: 30 }, () => "y".repeat(500)), n: 30 });
  assert.equal(big.length, 31);
});

// A synthetic GitHub token: redactSecrets matches its shape.
const GH_TOKEN = "ghp_" + "A1b2C3d4E5".repeat(3) + "F6g7H8";
const GH_TAIL = GH_TOKEN.slice(4, 24); // what a clipped token would still leak

test("SessionEnd body from a transcript carries no captured secret", () => {
  const transcript = [
    { type: "user", message: { content: `deploy with ${GH_TOKEN} please` } },
    {
      type: "assistant",
      message: {
        content: [
          { type: "text", text: `Using ${GH_TOKEN} now.` },
          { type: "tool_use", id: "t1", name: "mcp__loom__loom_memory_set", input: { value: `ci token ${GH_TOKEN}` } },
        ],
      },
    },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t1", content: `stored ${GH_TOKEN}` }] } },
  ];
  const echoes = sessionToolEchoes(transcript);
  assert.ok(echoes.values.length >= 2);
  const body = sessionMemoryBody(scope, { session_id: "s1" }, transcriptTurns(transcript), { echoes });
  const wire = JSON.stringify(body);
  assert.ok(!wire.includes(GH_TAIL), wire);
  assert.ok(wire.includes("[redacted]"));
});

test("SessionEnd body from the journal carries no captured secret, even at the clip edge", () => {
  const pad = "x".repeat(40);
  const rows = parseJournal(
    [
      JSON.stringify({ role: "user", content: `${pad} ${GH_TOKEN}`, at: "2026-09-12T10:00:00Z" }),
      JSON.stringify({ role: "assistant", content: `ok, ${GH_TOKEN}`, at: "2026-09-12T10:00:01Z" }),
    ].join("\n"),
  );
  // The clip falls inside the first turn's token: redacting after it would leak the tail.
  const body = sessionMemoryBody(scope, { session_id: "s1" }, journalTurns(rows), { maxMessageChars: 80 });
  const wire = JSON.stringify(body);
  assert.ok(!wire.includes(GH_TAIL), wire);
  assert.equal(body.messages.length, 2);
});
