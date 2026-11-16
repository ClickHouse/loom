const assert = require("node:assert/strict");
const { afterEach, mock, test } = require("node:test");
const { Agent } = require("@clickhouse/loom");
const { HumanMessage, AIMessage } = require("@langchain/core/messages");
const { Loom } = require("../dist/nodes/Loom/Loom.node.js");
const { LoomMemory } = require("../dist/nodes/LoomMemory/LoomMemory.node.js");
const { LoomChatHistory } = require("../dist/nodes/LoomMemory/LoomChatHistory.js");

afterEach(() => mock.restoreAll());

function harness({ unserved = 0, cap = 500 } = {}) {
  const rows = new Map();
  const calls = [];
  const key = (body) =>
    JSON.stringify([
      body.org,
      body.namespace,
      body.agent,
      body.user_id,
      body.session_id,
      body.memory_key,
    ]);
  const fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    const op = String(url).split("/v1/")[1];
    calls.push({ op, body });
    let output = {};
    if (op === "memory.set") {
      rows.set(key(body), { ...body, value: JSON.parse(body.value) });
      output = { memory_id: key(body) };
    } else if (op === "memory.list") {
      const mine = [...rows.values()].filter(
        (r) =>
          r.org === body.org &&
          r.namespace === body.namespace &&
          r.agent === body.agent &&
          r.user_id === body.user_id &&
          r.session_id === body.session_id &&
          r.memory_key.startsWith(body.prefix),
      );
      const scanned = [...Array.from({ length: unserved }, () => null), ...mine.reverse()];
      const limit = Math.min(cap, body.limit);
      const page = scanned.slice(body.offset, body.offset + limit).filter(Boolean);
      output = {
        entries: page.map((r) => {
          const text = JSON.stringify(r.value);
          return {
            memory_key: r.memory_key,
            value: text.slice(0, 2048),
            value_truncated: text.length > 2048,
          };
        }),
        truncated: body.offset + limit < scanned.length,
      };
    } else if (op === "memory.get") {
      output = rows.has(key(body))
        ? { found: true, value: rows.get(key(body)).value }
        : { found: false };
    } else if (op === "memory.delete") {
      rows.delete(key(body));
      output = { deleted: true };
    } else if (op === "memory.search") {
      output = { entries: [{ memory_key: "seat", value: "aisle" }], abstain: false };
    } else if (op === "knowledge.search") {
      output = { entries: [{ title: "Travel", content: "Aisle seats" }] };
    }
    return new Response(JSON.stringify(output), { status: 200 });
  };
  mock.method(globalThis, "fetch", fetch);
  const agent = new Agent("org", "ns", "n8n", { baseUrl: "http://loom.test", logLevel: "off" });
  const context = (params = {}, overrides = {}) => ({
    getCredentials: async () => ({ baseUrl: "http://loom.test", apiKey: "test-token" }),
    getNodeParameter: (name) =>
      ({
        org: "org",
        namespace: "ns",
        agent: "n8n",
        userId: "alice",
        sessionId: "trip",
        contextWindowLength: 2,
        ...params,
      })[name],
    getWorkflow: () => ({ id: "wf1" }),
    getInputData: () => [{ json: {} }],
    getNode: () => ({ name: "Loom", typeVersion: 1 }),
    continueOnFail: () => false,
    ...overrides,
  });
  return { agent, rows, calls, context };
}

test("history keeps repeats and long messages in order across server-capped filtered pages", async () => {
  const { agent } = harness({ unserved: 3, cap: 2 });
  const history = new LoomChatHistory(agent, "trip", "alice");
  const messages = [
    new HumanMessage("hi"),
    new AIMessage("x".repeat(5000)),
    new HumanMessage("hi"),
  ];
  for (const message of messages) await history.addMessage(message);
  const resumed = new LoomChatHistory(agent, "trip", "alice");
  const got = await resumed.getMessages();
  assert.deepEqual(
    got.map((m) => [m.getType(), m.content]),
    messages.map((m) => [m.getType(), m.content]),
  );
});

test("clearing a conversation leaves other users and sessions intact", async () => {
  const { agent } = harness();
  const mine = new LoomChatHistory(agent, "trip", "alice");
  const otherUser = new LoomChatHistory(agent, "trip", "bob");
  const otherSession = new LoomChatHistory(agent, "another", "alice");
  for (const history of [mine, otherUser, otherSession])
    await history.addMessage(new HumanMessage("hi"));
  await mine.clear();
  assert.deepEqual(await mine.getMessages(), []);
  assert.equal((await otherUser.getMessages())[0].content, "hi");
  assert.equal((await otherSession.getMessages())[0].content, "hi");
});

test("the real framework memory saves turns and returns its configured window after resumption", async () => {
  const { context, rows } = harness();
  const node = new LoomMemory();
  const { response: memory } = await node.supplyData.call(context({ contextWindowLength: 1 }), 0);
  await memory.saveContext({ input: "first" }, { output: "one" });
  await memory.saveContext({ input: "second" }, { output: "two" });
  const { response: resumed } = await node.supplyData.call(context({ contextWindowLength: 1 }), 0);
  assert.deepEqual(
    (await resumed.loadMemoryVariables({ input: "third" })).chat_history.map((m) => m.content),
    ["second", "two"],
  );
  assert.equal(rows.size, 4);
  const { response: otherWorkflow } = await node.supplyData.call(
    context({}, { getWorkflow: () => ({ id: "wf2" }) }),
    0,
  );
  assert.deepEqual((await otherWorkflow.loadMemoryVariables({})).chat_history, []);
});

test("the action node returns Loom search results and binds writes to the user", async () => {
  const { context, calls } = harness();
  const node = new Loom();
  const [remembered] = await node.execute.call(
    context({ operation: "remember", memoryKey: "seat", value: '"aisle"' }),
  );
  assert.ok(remembered[0].json.memory_id);
  assert.equal(calls[0].body.user_id, "alice");
  const [recalled] = await node.execute.call(
    context({ operation: "recall", query: "seat", limit: 5 }),
  );
  assert.equal(recalled[0].json.entries[0].value, "aisle");
  const [knowledge] = await node.execute.call(
    context({ operation: "searchKnowledge", query: "seat", limit: 5 }),
  );
  assert.equal(knowledge[0].json.entries[0].title, "Travel");
  for (const call of calls.filter((call) =>
    ["memory.search", "knowledge.search"].includes(call.op),
  )) {
    assert.equal(call.body.namespace_scope, "this");
    assert.equal(call.body.user_id, "alice");
  }
  assert.equal(node.description.usableAsTool, true);
});

test("server refusals fail a node or become an error item when continue-on-fail is selected", async () => {
  const { context } = harness();
  mock.method(
    globalThis,
    "fetch",
    async () => new Response(JSON.stringify({ detail: "forbidden" }), { status: 403 }),
  );
  const node = new Loom();
  const params = { operation: "recall", query: "seat", limit: 5 };
  await assert.rejects(node.execute.call(context(params)), /forbidden|403/);
  const [items] = await node.execute.call(context(params, { continueOnFail: () => true }));
  assert.match(items[0].json.error, /forbidden|403/);
});

test("the bundled tokenizer carries its upstream license text", () => {
  const { readFileSync } = require("node:fs");
  const licenses = readFileSync(
    require("node:path").join(__dirname, "../dist/THIRD_PARTY_LICENSES.txt"),
    "utf8",
  );
  const tokenizer = licenses.split("js-tiktoken 1.0.21\n")[1]?.split("\n\n---\n\n")[0];
  assert.match(tokenizer, /Permission is hereby granted/);
});

test("the memory node refuses missing and non-primitive conversation ids", async () => {
  const { context } = harness();
  const node = new LoomMemory();
  for (const sessionId of [undefined, null, {}, [], "", " ", false]) {
    await assert.rejects(node.supplyData.call(context({ sessionId }), 0), /Session ID/);
  }
  for (const sessionId of [0, "valid"]) {
    const supplied = await node.supplyData.call(context({ sessionId }), 0);
    assert.ok(supplied.response);
  }
});
