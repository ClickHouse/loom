const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const { test } = require("node:test");
const { Agent } = require("@clickhouse/loom");
const { dirname, join } = require("node:path");
const root = dirname(require.resolve("@clickhouse/loom/package.json"));
const { HumanMessage, AIMessage } = require("@langchain/core/messages");
const { LoomChatHistory } = require(join(root, "dist/integrations/n8n/nodes/LoomMemory/LoomChatHistory.js"));
const { LoomMemory } = require(join(root, "dist/integrations/n8n/nodes/LoomMemory/LoomMemory.node.js"));

test("conversation messages are stored, resumed and cleared in a local Loom deployment", {
  skip: !process.env.LOOM_BASE_URL,
}, async () => {
  const org = `it-n8n-${randomUUID().slice(0, 8)}`;
  const agent = new Agent(org, "default", "n8n", {
    baseUrl: process.env.LOOM_BASE_URL,
    apiKey: process.env.LOOM_API_KEY || undefined,
    logLevel: "off",
  });
  const history = new LoomChatHistory(agent, "trip", "alice");
  const memoryFor = async (userId) => {
    const context = {
      getCredentials: async () => ({ baseUrl: process.env.LOOM_BASE_URL, apiKey: process.env.LOOM_API_KEY || "" }),
      getNodeParameter: (name) =>
        ({ org, namespace: "default", agent: "n8n", userId, sessionId: "normalized", contextWindowLength: 2 })[name],
      getWorkflow: () => ({ id: "normalization-check" }),
    };
    return (await new LoomMemory().supplyData.call(context, 0)).response;
  };
  let memory;
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
    for (const edge of ["\u0085", "\u001c", "\u001d", "\u001e", "\u001f"]) {
      memory = await memoryFor(`${edge}alice${edge}`);
      await memory.saveContext({ input: "x".repeat(5000) }, { output: "saved" });
      const canonical = await memoryFor("alice");
      assert.deepEqual(
        (await canonical.loadMemoryVariables({})).chat_history.map((message) => message.content),
        ["x".repeat(5000), "saved"],
      );
      await memory.clear();
    }
  } finally {
    if (memory) await memory.clear();
    await history.clear();
  }
});
