import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LoomMemoryStore } from "./strands.js";

function harness(searchReply: unknown = { results: [] }) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    calls.push({ op, body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(op === "memory.search" ? searchReply : { version: 1 }), { status: 200 });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

describe("LoomMemoryStore (Strands adapter)", () => {
  it("has the fields Strands' MemoryManager reads", () => {
    const store = new LoomMemoryStore(harness().agent);
    expect([store.name, store.writable, store.maxSearchResults]).toEqual(["loom", true, 3]);
    expect(store.description).toMatch(/loom/);
  });

  it("adds one content-keyed memory, with the key the Python adapter writes for the same text", async () => {
    const { agent, calls } = harness();
    const store = new LoomMemoryStore(agent, { tags: ["customer"], scope: { userId: "u1" } });
    await store.add("  Prefers an aisle seat ", {
      source: "chat",
      kind: "preference",
      score: 3,
      topic: "x".repeat(65),
    });
    await store.add("   ");
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("memory.set");
    expect(calls[0].body).toMatchObject({
      memory_key: "strands/d28cd022544d2a0d2666",
      value: "Prefers an aisle seat",
      memory_type: "semantic",
      user_id: "u1",
      tags: ["customer", "kind:preference", "source:chat"],
    });
  });

  it("searches within its tags and type and returns entries with the hit's key and score", async () => {
    const { agent, calls } = harness({
      results: [
        {
          memory_key: "k1",
          memory_type: "semantic",
          content_excerpt: "aisle seat",
          similarity: 0.912345,
          source: "chat",
        },
        { memory_key: "k2", memory_type: "semantic", content_excerpt: "window", similarity: 0.5 },
      ],
    });
    const store = new LoomMemoryStore(agent, { tags: ["customer"], memoryType: "preference" });
    const hits = await store.search("seat", { maxSearchResults: 1 });
    expect(calls[0].body).toMatchObject({
      query: "seat",
      top_k: 1,
      bodies: "full", // whole bodies whatever the top_k
      explain: false,
      tags: ["customer"],
      memory_type: "preference",
    });
    expect(hits).toEqual([
      {
        content: "aisle seat",
        storeName: "loom",
        metadata: { memory_key: "k1", memory_type: "semantic", similarity: 0.9123, source: "chat" },
      },
    ]);
  });

  it("leaves out a hit below the floor, unless told to keep it", async () => {
    const reply = {
      results: [
        { memory_key: "k1", memory_type: "semantic", content_excerpt: "aisle", similarity: 0.9 },
        { memory_key: "k2", memory_type: "semantic", content_excerpt: "x", similarity: 0.2, below_threshold: true },
        { memory_key: "k3", memory_type: "semantic", content_excerpt: "espresso", similarity: 0.8 },
      ],
    };
    // three rows for two results: the floor is applied before the cut
    const strict = await new LoomMemoryStore(harness(reply).agent, { maxSearchResults: 2 }).search("q");
    expect(strict.map((e) => e.metadata?.memory_key)).toEqual(["k1", "k3"]);
    const loose = await new LoomMemoryStore(harness(reply).agent, { maxSearchResults: 2, honourAbstain: false }).search(
      "q",
    );
    expect(loose.map((e) => e.metadata?.memory_key)).toEqual(["k1", "k2"]);
  });

  it("returns nothing when loom abstains, unless told to keep the hits", async () => {
    const reply = {
      abstain: true,
      results: [{ memory_key: "k", memory_type: "semantic", content_excerpt: "x", similarity: 0.2 }],
    };
    expect(await new LoomMemoryStore(harness(reply).agent).search("q")).toEqual([]);
    expect(await new LoomMemoryStore(harness(reply).agent, { honourAbstain: false }).search("q")).toHaveLength(1);
  });
});
