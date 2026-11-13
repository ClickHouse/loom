/**
 * The LangChain.js retrievers against a running Loom (same environment as
 * live.test.ts): a document and a memory written through the SDK come back as
 * LangChain Documents.
 */
import { describe, expect, it } from "vitest";
import { Datapack, LoomClient } from "../../src/index.js";
import { LoomKnowledgeRetriever, LoomMemoryRetriever } from "../../src/integrations/langchain.js";

const base = process.env.LOOM_BASE_URL ?? "";
const key = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const ns = process.env.LOOM_NAMESPACE ?? `ts-sdk-langchain-${Math.random().toString(16).slice(2, 8)}`;

describe.skipIf(!base || !key)("LangChain.js retrievers on live loom", () => {
  const client = new LoomClient({ baseUrl: base, apiKey: key, consumer: "ts-sdk-integ" });
  const bot = new Datapack(org, ns, client).agent("langchain-bot");

  it("returns an added document chunk", async () => {
    const uri = `doc://refunds-${Date.now()}`;
    await bot.call("knowledge.add", {
      content: "Annual plans are refunded pro rata within thirty days of renewal.",
      source_uri: uri,
      title: "Refunds",
    } as never);
    const docs = await new LoomKnowledgeRetriever(bot, { uriPrefix: [uri] }).invoke("annual plan refund window");
    expect(docs.map((d) => d.id)).toEqual([`${uri}#0`]);
    expect(docs[0].pageContent).toContain("pro rata");
  });

  it("returns a remembered memory", async () => {
    await bot.remember("prefs/seat", "Alice prefers aisle seats on long flights");
    const docs = await new LoomMemoryRetriever(bot, { honourAbstain: false }).invoke("Alice aisle seats long flights");
    expect(docs[0]?.id).toBe("prefs/seat");
  });
});
