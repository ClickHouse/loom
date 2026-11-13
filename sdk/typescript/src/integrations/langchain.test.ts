import { BaseRetriever } from "@langchain/core/retrievers";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LoomKnowledgeRetriever, LoomMemoryRetriever } from "./langchain.js";

function harness(reply: unknown) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ op: url.split("/v1/")[1], body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

const chunk = {
  source_uri: "doc://refunds",
  chunk_idx: 2,
  content: "Annual plans refund pro rata.",
  tags: ["policy"],
  distance: 0.09,
  similarity: 0.912345,
  section: "Refunds",
};

describe("LoomKnowledgeRetriever (LangChain.js adapter)", () => {
  it("is a LangChain retriever", () => {
    expect(new LoomKnowledgeRetriever(harness({}).agent)).toBeInstanceOf(BaseRetriever);
    expect(new LoomMemoryRetriever(harness({}).agent)).toBeInstanceOf(BaseRetriever);
  });

  it("returns one Document per chunk, with where it came from", async () => {
    const { agent, calls } = harness({ results: [chunk], embedding_model: "m" });
    const docs = await new LoomKnowledgeRetriever(agent, { k: 3 }).invoke("refund policy");
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("knowledge.search");
    expect(calls[0].body).toMatchObject({
      org: "o",
      namespace: "n",
      query: "refund policy",
      top_k: 3,
      namespace_scope: "this",
    });
    expect(calls[0].body).not.toHaveProperty("tags");
    expect(docs).toHaveLength(1);
    expect(docs[0].pageContent).toBe("Annual plans refund pro rata.");
    expect(docs[0].id).toBe("n/doc://refunds#2");
    expect(docs[0].metadata).toEqual({
      source_uri: "doc://refunds",
      chunk_idx: 2,
      similarity: 0.9123,
      section: "Refunds",
      namespace: "n",
      tags: ["policy"],
    });
  });

  it("names a fanned-out hit by its own namespace", async () => {
    const { agent } = harness({ results: [{ ...chunk, namespace: "other" }], embedding_model: "m" });
    const docs = await new LoomKnowledgeRetriever(agent, { namespaceScope: "all_mine" }).invoke("q");
    expect(docs[0].id).toBe("other/doc://refunds#2");
    expect(docs[0].metadata.namespace).toBe("other");
  });

  it("sends its filters, and k wins over a top_k in searchKwargs", async () => {
    const { agent, calls } = harness({ results: [], embedding_model: "m" });
    await new LoomKnowledgeRetriever(agent, {
      searchTags: ["policy"],
      uriPrefix: ["doc://handbook/"],
      namespaceScope: "all_mine",
      searchKwargs: { search_mode: "lexical", top_k: 99 },
    }).invoke("refunds");
    expect(calls[0].body).toMatchObject({
      search_mode: "lexical",
      top_k: 4,
      tags: ["policy"],
      uri_prefix: ["doc://handbook/"],
      namespace_scope: "all_mine",
    });
  });
});

describe("LoomMemoryRetriever (LangChain.js adapter)", () => {
  const memories = {
    results: [
      {
        memory_key: "prefs/seat",
        memory_type: "semantic",
        content_excerpt: "Alice prefers aisle seats",
        similarity: 0.9,
        source: "fact",
        valid_at: "2026-09-01",
      },
      { memory_key: "prefs/meal", memory_type: "semantic", content_excerpt: "vegetarian", similarity: 0.7 },
    ],
  };

  it("returns one Document per memory within k, with its key in metadata", async () => {
    const { agent, calls } = harness(memories);
    const docs = await new LoomMemoryRetriever(agent, {
      k: 1,
      searchTags: ["customer"],
      memoryType: "semantic",
      scope: { userId: "alice" },
    }).invoke("seating");
    expect(calls[0].op).toBe("memory.search");
    expect(calls[0].body).toMatchObject({
      query: "seating",
      top_k: 1,
      tags: ["customer"],
      memory_type: "semantic",
      user_id: "alice",
    });
    expect(calls[0].body.agent).toBe("*"); // every agent's memories unless the scope names one
    expect(docs.map((d) => d.metadata.memory_key)).toEqual(["prefs/seat"]);
    expect(docs[0].id).toBeUndefined();
    expect(docs[0].metadata).toEqual({
      namespace: "n",
      memory_key: "prefs/seat",
      memory_type: "semantic",
      similarity: 0.9,
      source: "fact",
      valid_at: "2026-09-01",
    });
  });

  it("leaves out a memory below the floor, unless told not to", async () => {
    const reply = {
      results: [memories.results[0], { ...memories.results[1], below_threshold: true }],
    };
    const strict = await new LoomMemoryRetriever(harness(reply).agent).invoke("q");
    expect(strict.map((d) => d.metadata.memory_key)).toEqual(["prefs/seat"]);
    const loose = await new LoomMemoryRetriever(harness(reply).agent, { honourAbstain: false }).invoke("q");
    expect(loose.map((d) => d.metadata.memory_key)).toEqual(["prefs/seat", "prefs/meal"]);
  });

  it("returns nothing when Loom abstains, unless told not to", async () => {
    const reply = { ...memories, abstain: true };
    expect(await new LoomMemoryRetriever(harness(reply).agent).invoke("q")).toEqual([]);
    const loose = await new LoomMemoryRetriever(harness(reply).agent, { honourAbstain: false }).invoke("q");
    expect(loose.map((d) => d.metadata.memory_key)).toEqual(["prefs/seat", "prefs/meal"]);
    expect(loose[1].metadata.source).toBe("fact");
  });
});
