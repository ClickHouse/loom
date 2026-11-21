/** The native ADK memory service reads user facts and shared app documents from Loom. */
import { describe, expect, it } from "vitest";
import { Datapack, LoomClient } from "../../src/index.js";
import { LoomMemoryService } from "../../src/integrations/google-adk.js";

const baseUrl = process.env.LOOM_BASE_URL ?? "";
const apiKey = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const namespace = `ts-adk-${Math.random().toString(16).slice(2)}`;

describe.skipIf(!baseUrl || !apiKey)("Google ADK on live Loom", () => {
  it("returns user facts and namespace documents through the service", async () => {
    const client = new LoomClient({ baseUrl, apiKey, consumer: "ts-sdk-integ" });
    const agent = new Datapack(org, namespace, client).agent("adk-bot");
    const memory = new LoomMemoryService(agent, { appName: "travel", honourAbstain: false });
    await agent.call("memory.set", { memory_key: "seat", value: "Alice prefers aisle seats" }, { userId: "alice" });
    await agent.call("knowledge.upsert", {
      source_uri: "travel-guide",
      content: "The travel guide describes aisle seats and boarding.",
    });
    let sources: unknown[] = [];
    for (let i = 0; i < 30; i++) {
      const result = await memory.searchMemory({ appName: "travel", userId: "alice", query: "aisle seats" });
      sources = result.memories.map((entry) => entry.customMetadata?.source);
      if (sources.includes("fact") && sources.includes("knowledge")) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(sources).toContain("fact");
    expect(sources).toContain("knowledge");
    const bob = await memory.searchMemory({ appName: "travel", userId: "bob", query: "aisle seats" });
    expect(bob.memories.some((entry) => entry.customMetadata?.source === "fact")).toBe(false);
    expect(bob.memories.some((entry) => entry.customMetadata?.source === "knowledge")).toBe(true);
  });
});
