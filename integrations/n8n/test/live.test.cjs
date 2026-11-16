const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const { test } = require("node:test");
const { Agent } = require("@clickhouse/loom");
const { HumanMessage, AIMessage } = require("@langchain/core/messages");
const { LoomChatHistory } = require("../dist/nodes/LoomMemory/LoomChatHistory.js");

test("conversation messages are stored, resumed and cleared in a local Loom deployment", {
  skip: !process.env.LOOM_BASE_URL,
}, async () => {
  const agent = new Agent(`it-n8n-${randomUUID().slice(0, 8)}`, "default", "n8n", {
    baseUrl: process.env.LOOM_BASE_URL,
    apiKey: process.env.LOOM_API_KEY || undefined,
    logLevel: "off",
  });
  const history = new LoomChatHistory(agent, "trip", "alice");
  try {
    await history.addMessage(new HumanMessage("I prefer an aisle seat"));
    await history.addMessage(new AIMessage("x".repeat(5000)));
    const resumed = new LoomChatHistory(agent, "trip", "alice");
    assert.deepEqual(
      (await resumed.getMessages()).map((m) => m.content),
      ["I prefer an aisle seat", "x".repeat(5000)],
    );
    assert.deepEqual(await new LoomChatHistory(agent, "trip", "bob").getMessages(), []);
    await resumed.clear();
    assert.deepEqual(await history.getMessages(), []);
  } finally {
    await history.clear();
  }
});
