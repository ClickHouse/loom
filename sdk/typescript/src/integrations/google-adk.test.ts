import type { BaseLlmConnection, LlmRequest, LlmResponse } from "@google/adk";
import {
  BaseLlm,
  createEvent,
  createSession,
  InMemorySessionService,
  LlmAgent,
  PRELOAD_MEMORY,
  Runner,
} from "@google/adk";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LoomMemoryService } from "./google-adk.js";

function harness(replies: Record<string, unknown> = {}) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    calls.push({ op, body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(replies[op] ?? { results: [] }), { status: 200 });
  };
  const agent = new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" });
  return { calls, service: new LoomMemoryService(agent, { appName: "app", topK: 2, defer: false }), agent };
}
const fact = {
  agent: "writer-a",
  memory_key: "seat",
  session_id: "s",
  content_excerpt: "aisle",
  source: "fact",
  similarity: 0.9,
};

describe("Google ADK LoomMemoryService", () => {
  it.each(["fact", "anchor"])("distinguishes %s writers and leaves unknown rows without ids", async (source) => {
    const { service } = harness({
      "memory.search": {
        results: [
          { ...fact, source, agent: "writer-a" },
          { ...fact, source, agent: "writer-b" },
        ],
      },
    });
    const found = await service.searchMemory({ appName: "app", userId: "alice", query: "seat" });
    expect(found.memories[0].id).not.toBe(found.memories[1].id);
    const unknown = harness({ "memory.search": { results: [{ ...fact, source, agent: "" }] } });
    expect(
      (await unknown.service.searchMemory({ appName: "app", userId: "alice", query: "seat" })).memories[0].id,
    ).toBeUndefined();
  });
  it("returns the user's memories then app knowledge in the bound namespace", async () => {
    const { service, calls } = harness({
      "memory.search": { results: [fact] },
      "knowledge.search": { results: [{ content: "boarding guide", source_uri: "guide", chunk_idx: 0 }] },
    });
    const { memories } = await service.searchMemory({ appName: "app", userId: "alice", query: "travel" });
    expect(memories.map((m) => m.content.parts?.[0].text)).toEqual(["aisle", "boarding guide"]);
    expect(memories[0].id).toBe(JSON.stringify(["seat", "s", "writer-a"]));
    expect(memories[1].id).toBeUndefined();
    expect(calls[0].body).toMatchObject({ user_id: "alice", namespace_scope: "this", bodies: "full", top_k: 2 });
    expect(calls[1].body).toMatchObject({ namespace_scope: "this", top_k: 1 });
  });

  it("keeps knowledge when memory abstains and filters below-floor memories", async () => {
    const { service } = harness({
      "memory.search": { results: [fact], abstain: true },
      "knowledge.search": { results: [{ content: "guide" }] },
    });
    expect((await service.searchMemory({ appName: "app", userId: "alice", query: "travel" })).memories).toHaveLength(1);
    const h = harness({ "memory.search": { results: [{ ...fact, below_threshold: true }] } });
    expect((await h.service.searchMemory({ appName: "app", userId: "alice", query: "travel" })).memories).toEqual([]);
  });

  it.each(["*", ...["\u001c", "\u001d", "\u001e", "\u001f", "\u0085"].flatMap((c) => [c, `${c}alice`, `alice${c}`])])(
    "refuses a noncanonical or wildcard user %j before calling Loom",
    async (userId) => {
      const { service, calls } = harness();
      await expect(service.searchMemory({ appName: "app", userId, query: "seat" })).rejects.toThrow("userId");
      await expect(
        service.addSessionToMemory({ appName: "app", userId, id: "s", events: [], state: {}, lastUpdateTime: 0 }),
      ).rejects.toThrow("userId");
      expect(calls).toHaveLength(0);
    },
  );

  it("refuses app/user scope mistakes before calling Loom and skips blank queries", async () => {
    const { service, calls } = harness();
    for (const userId of ["", " ", "-", " - ", " alice", "alice ", "\u00a0alice"])
      await expect(service.searchMemory({ appName: "app", userId, query: "q" })).rejects.toThrow("userId");
    await expect(service.searchMemory({ appName: "other", userId: "alice", query: "q" })).rejects.toThrow("namespace");
    expect(await service.searchMemory({ appName: "app", userId: "alice", query: " " })).toEqual({ memories: [] });
    expect(calls).toEqual([]);
  });

  it("extracts only text, maps roles/timestamps, batches at400 and derives user/session scope", async () => {
    const { service, calls } = harness();
    const session = createSession({
      id: "shared",
      appName: "app",
      userId: "alice",
      events: [
        createEvent({
          author: "user",
          timestamp: 1700000000000,
          content: { parts: [{ text: "hello" }, { text: "private thought", thought: true }] },
        }),
        createEvent({ author: "bot", timestamp: 1700000001000, content: { parts: [{ text: "reply" }] } }),
        createEvent({ author: "bot", content: { parts: [{ functionCall: { name: "call" } }] } }),
      ],
    });
    await service.addSessionToMemory(session);
    expect(calls[0].body).toMatchObject({
      user_id: "alice",
      defer: false,
      messages: [
        { role: "user", content: "hello", timestamp: "2023-11-14T22:13:20.000Z" },
        { role: "assistant", content: "reply", timestamp: "2023-11-14T22:13:21.000Z" },
      ],
    });
    const alice = calls[0].body.session_id;
    await service.addSessionToMemory({ ...session, userId: "bob" });
    expect(calls[1].body.session_id).not.toBe(alice);
    session.events = Array.from({ length: 401 }, () => session.events[0]);
    await service.addSessionToMemory(session);
    expect((calls[2].body.messages as unknown[]).length).toBe(400);
    expect((calls[3].body.messages as unknown[]).length).toBe(1);
    expect(calls[2].body.session_id).toBe(calls[3].body.session_id);
  });

  it("caps the knowledge leg at its100-result wire limit", async () => {
    const { agent, calls } = harness();
    const service = new LoomMemoryService(agent, { appName: "app", topK: 500 });
    await service.searchMemory({ appName: "app", userId: "alice", query: "guide" });
    expect(calls[1].body.top_k).toBe(100);
  });

  it("bounds topK and refuses empty session ids", async () => {
    const { agent, service } = harness();
    for (const topK of [0, 501, 1.5])
      expect(() => new LoomMemoryService(agent, { appName: "app", topK })).toThrow("topK");
    await expect(
      service.addSessionToMemory(createSession({ id: "", appName: "app", userId: "alice" })),
    ).rejects.toThrow("session id");
  });

  it("preloads memories into a real Runner model request without a provider call", async () => {
    const { service, calls } = harness({ "memory.search": { results: [fact] } });
    class ScriptedModel extends BaseLlm {
      seen = "";
      constructor() {
        super({ model: "scripted" });
      }
      async *generateContentAsync(request: LlmRequest): AsyncGenerator<LlmResponse, void> {
        this.seen = JSON.stringify(request);
        yield { content: { role: "model", parts: [{ text: "done" }] }, turnComplete: true };
      }
      async connect(): Promise<BaseLlmConnection> {
        throw new Error("unused");
      }
    }
    const model = new ScriptedModel();
    const runner = new Runner({
      appName: "app",
      agent: new LlmAgent({ name: "assistant", model, tools: [PRELOAD_MEMORY] }),
      sessionService: new InMemorySessionService(),
      memoryService: service,
      autoCreateSession: true,
    });
    const events = [];
    for await (const event of runner.runAsync({
      userId: "alice",
      sessionId: "turn",
      newMessage: { role: "user", parts: [{ text: "travel" }] },
    }))
      events.push(event);
    expect(model.seen).toContain("aisle");
    expect(events.length).toBeGreaterThan(0);
    expect(calls[0].body.user_id).toBe("alice");
  });
});
