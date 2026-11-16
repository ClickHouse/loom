/** Loom's verbs as the model's tools, in each provider's shape, with one dispatcher. */
import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { LoomClient } from "./client.js";
import { LoomError } from "./errors.js";
import { LOOM_TOOL_SPECS, toolDefinitions } from "./tools.js";
import { Transport } from "./transport.js";

function harness(replies: Record<string, unknown>) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    const op = url.split("/v1/")[1];
    calls.push({ op, body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify(replies[op] ?? {}), { status: 200 });
  };
  const bot = new Agent(
    "acme",
    "support",
    "bot",
    new LoomClient(new Transport({ baseUrl: "http://x", fetch, logLevel: "off" })),
  );
  return { bot, calls };
}

describe("tool definitions", () => {
  it("come in anthropic, openai and generic shapes with the same six tools", () => {
    expect(LOOM_TOOL_SPECS.map((s) => s.name)).toEqual([
      "loom_remember",
      "loom_recall",
      "loom_ask",
      "loom_offload",
      "loom_fetch",
      "loom_note",
    ]);
    const a = toolDefinitions("anthropic");
    expect(a[0]).toMatchObject({
      name: "loom_remember",
      input_schema: { type: "object", required: ["memory_key", "value"] },
    });
    const o = toolDefinitions("openai", ["loom_ask"]);
    expect(o).toHaveLength(1);
    expect(o[0].type).toBe("function");
    expect(o[0].function.name).toBe("loom_ask");
    const g = toolDefinitions();
    expect(g).toHaveLength(6);
    for (const s of LOOM_TOOL_SPECS) {
      expect(s.description.length).toBeGreaterThan(40);
      expect(s.parameters.additionalProperties).toBe(false);
      for (const r of s.parameters.required ?? []) expect(s.parameters.properties).toHaveProperty(r);
    }
  });
});

describe("AgentTools.execute", () => {
  it("runs each tool under the scope and returns the string the model reads", async () => {
    const { bot, calls } = harness({
      "memory.set": { memory_key: "seat", version: 3, conflict_verdict: "new" },
      "memory.search": {
        results: [
          {
            memory_key: "seat",
            content_excerpt: "Prefers an aisle seat",
            similarity: 0.51234,
            memory_type: "preference",
            distance: 0.49,
          },
        ],
      },
      "memory.ask": { answer: "An aisle seat.", abstain: false, memory_keys: ["seat"] },
      "context.offload": { blob_key: "blob/1", pointer: "[offloaded → context.fetch blob_key=blob/1] log" },
      "context.fetch": { found: true, blob_key: "blob/1", content: "line", next_offset: -1, matched_lines: 1 },
      "working_memory.append": { block_key: "main", version: 1, char_limit: 4000, value_length: 5, fill_ratio: 0.01 },
    });
    const tools = bot.tools({ userId: "u1", sessionId: "s1" });
    expect(tools.definitions("openai")).toHaveLength(6);

    expect(
      JSON.parse(
        await tools.execute("loom_remember", {
          memory_key: "seat",
          value: "Prefers an aisle seat",
          memory_type: "preference",
        }),
      ),
    ).toEqual({
      stored: true,
      memory_key: "seat",
      version: 3,
      verdict: "new",
    });
    expect(calls[0].body).toMatchObject({
      memory_key: "seat",
      memory_type: "preference",
      user_id: "u1",
      session_id: "s1",
    });

    expect(JSON.parse(await tools.execute("loom_recall", '{"query":"seat","top_k":3}'))).toEqual({
      memories: [{ memory_key: "seat", content: "Prefers an aisle seat", similarity: 0.512 }],
    });
    expect(calls[1].body).toMatchObject({ query: "seat", top_k: 3, bodies: "full", explain: false });

    expect(JSON.parse(await tools.execute("loom_ask", { query: "which seat?" }))).toEqual({
      answer: "An aisle seat.",
      abstain: false,
      cited: ["seat"],
    });
    expect(await tools.execute("loom_offload", { content: "log", summary: "a log" })).toContain("blob_key=blob/1");
    expect(JSON.parse(await tools.execute("loom_fetch", { blob_key: "blob/1", pattern: "FAILED" }))).toEqual({
      content: "line",
      next_offset: -1,
      matched_lines: 1,
    });
    expect(JSON.parse(await tools.execute("loom_note", { addition: "plan" }))).toMatchObject({
      noted: true,
      version: 1,
    });
  });

  it("rejects unknown tools, bad JSON and missing arguments with messages a model can act on", async () => {
    const { bot } = harness({ "context.fetch": { found: false, blob_key: "nope" } });
    const tools = bot.tools({}, { only: ["loom_recall", "loom_fetch"] });
    expect(tools.definitions()).toHaveLength(2);
    await expect(tools.execute("loom_remember", {})).rejects.toThrow(
      /unknown tool loom_remember; this agent offers loom_recall, loom_fetch/,
    );
    await expect(tools.execute("loom_recall", "{not json")).rejects.toThrow(/not valid JSON/);
    await expect(tools.execute("loom_recall", {})).rejects.toBeInstanceOf(LoomError);
    await expect(tools.execute("loom_recall", { query: "" })).rejects.toThrow(/query is required/);
    await expect(tools.execute("loom_recall", "[1, 2]")).rejects.toThrow(/must be a JSON object/);
    expect(await tools.execute("loom_fetch", { blob_key: "nope" })).toMatch(/no parked text under blob_key=nope/);
  });

  it("truncates a result past maxResultChars", async () => {
    const { bot } = harness({
      "context.fetch": { found: true, blob_key: "b", content: "x".repeat(500), next_offset: -1 },
    });
    const out = await bot.tools({}, { maxResultChars: 100 }).execute("loom_fetch", { blob_key: "b" });
    expect(out.length).toBeLessThan(120);
    expect(out.endsWith("…[truncated]")).toBe(true);
  });
});

describe("AgentTools argument checks", () => {
  it("checks the model's arguments against the tool's own schema before anything is sent", async () => {
    const { bot, calls } = harness({ "memory.search": { results: [] }, "context.fetch": { found: false } });
    const tools = bot.tools();
    for (const [name, args, message] of [
      ["loom_recall", { query: "q", top_k: "lots" }, /top_k must be an integer, got "lots"/],
      ["loom_recall", { query: "q", top_k: 50 }, /top_k must be within 1\.\.20, got 50/],
      ["loom_fetch", { blob_key: "b", offset: -3 }, /offset must be within 0\.\., got -3/],
      ["loom_fetch", { blob_key: "b", limit: 2.5 }, /limit must be an integer/],
      ["loom_remember", { memory_key: "k", value: "v", memory_type: "gossip" }, /memory_type must be one of semantic/],
      ["loom_remember", { memory_key: 7, value: "v" }, /memory_key must be a string, got 7/],
    ] as const) {
      await expect(tools.execute(name, args), JSON.stringify(args)).rejects.toThrow(message);
    }
    expect(calls).toHaveLength(0);
    await tools.execute("loom_recall", { query: "q", top_k: "3", unknown_extra: true });
    expect(calls.at(-1)?.body).toMatchObject({ query: "q", top_k: 3 });
    expect(calls.at(-1)?.body).not.toHaveProperty("unknown_extra");
  });

  it("drops whole recalled memories over the cap, so the result stays JSON", async () => {
    const results = [0, 1, 2, 3, 4].map((i) => ({
      memory_key: `m${i}`,
      content_excerpt: "x".repeat(400),
      similarity: 0.9 - i / 100,
    }));
    const { bot } = harness({ "memory.search": { results } });
    const tools = bot.tools({}, { only: ["loom_recall"], maxResultChars: 1_000 });
    const out = JSON.parse(await tools.execute("loom_recall", { query: "seat", top_k: 5 }));
    expect(out.memories.map((m: { memory_key: string }) => m.memory_key)).toEqual(["m0", "m1"]);
    expect(out.omitted).toBe(3);
  });

  it("keeps the best recalled memory when it alone is over the cap, and cuts it", async () => {
    const results = [0, 1].map((i) => ({ memory_key: `m${i}`, content_excerpt: "x".repeat(900), similarity: 0.9 }));
    const { bot } = harness({ "memory.search": { results } });
    const tools = bot.tools({}, { only: ["loom_recall"], maxResultChars: 500 });
    const out = await tools.execute("loom_recall", { query: "seat" });
    expect(out.endsWith("…[truncated]")).toBe(true);
    expect(out.startsWith('{"memories":[{"memory_key":"m0"')).toBe(true);
  });
});
