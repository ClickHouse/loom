import { Agent as MastraAgent } from "@mastra/core/agent";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LOOM_TOOL_SPECS } from "../tools.js";
import { loomTools } from "./mastra.js";

// Mastra reports usage to its own analytics unless this is set; a test sends nothing.
process.env.MASTRA_TELEMETRY_DISABLED = "1";

function harness(reply: unknown, status = 200) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ op: url.split("/v1/")[1], body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(reply), { status });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

/** A language model (AI SDK v2 spec) that answers each step from a script and keeps the prompts it saw. */
function scripted(steps: Array<Array<Record<string, unknown>>>) {
  const prompts: unknown[] = [];
  let step = 0;
  const usage = { inputTokens: 1, outputTokens: 1, totalTokens: 2 };
  const model = {
    specificationVersion: "v2",
    provider: "test",
    modelId: "scripted",
    supportedUrls: {},
    async doGenerate(options: { prompt: unknown }) {
      prompts.push(options.prompt);
      const parts = steps[Math.min(step++, steps.length - 1)];
      return {
        content: parts,
        finishReason: parts.some((p) => p.type === "tool-call") ? "tool-calls" : "stop",
        usage,
        warnings: [],
      };
    },
    async doStream(): Promise<never> {
      throw new Error("the scripted model answers generate only");
    },
  };
  return { model, prompts };
}

function recallThenAnswer(input: Record<string, unknown>) {
  return scripted([
    [{ type: "tool-call", toolCallId: "c1", toolName: "loom_recall", input: JSON.stringify(input) }],
    [{ type: "text", text: "Aisle." }],
  ]);
}

describe("loomTools (Mastra adapter)", () => {
  it("keys every verb by name, as a Mastra tool with Loom's description", () => {
    const tools = loomTools(harness({}).agent);
    expect(Object.keys(tools)).toEqual(LOOM_TOOL_SPECS.map((s) => s.name));
    for (const spec of LOOM_TOOL_SPECS) {
      expect(tools[spec.name].id).toBe(spec.name);
      expect(tools[spec.name].description).toBe(spec.description);
      const schema = tools[spec.name].inputSchema as unknown as {
        "~standard": { jsonSchema: { input: (o: { target: string }) => Record<string, unknown> } };
      };
      const { $schema: _draft, ...input } = schema["~standard"].jsonSchema.input({ target: "draft-07" });
      expect(input).toEqual(spec.parameters);
    }
  });

  it("keeps a subset with only", () => {
    expect(Object.keys(loomTools(harness({}).agent, {}, { only: ["loom_recall"] }))).toEqual(["loom_recall"]);
  });

  it("runs memory.search in the bound scope and returns the string the model reads", async () => {
    const { agent, calls } = harness({
      results: [{ memory_key: "seat", content_excerpt: "Alice prefers aisle", similarity: 0.91 }],
    });
    const { loom_recall } = loomTools(agent, { userId: "alice" }, { only: ["loom_recall"] });
    const out = await loom_recall.execute?.({ query: "seat preference", top_k: 3 }, {} as never);
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("memory.search");
    expect(calls[0].body).toMatchObject({ query: "seat preference", top_k: 3, user_id: "alice" });
    expect(JSON.parse(out as string)).toEqual({
      memories: [{ memory_key: "seat", content: "Alice prefers aisle", similarity: 0.91 }],
    });
  });

  it("throws Loom's message for a call Loom refuses", async () => {
    const { agent, calls } = harness({});
    const { loom_recall } = loomTools(agent, {}, { only: ["loom_recall"] });
    await expect(loom_recall.execute?.({ query: "" }, {} as never)).rejects.toThrow(
      "tool argument query is required and must be a non-empty string",
    );
    expect(calls).toHaveLength(0);
  });

  it("serves a Mastra agent's loom_recall call, and the model reads the result", async () => {
    const { agent, calls } = harness({
      results: [{ memory_key: "seat", content_excerpt: "Alice prefers aisle", similarity: 0.91 }],
    });
    const { model, prompts } = recallThenAnswer({ query: "seat" });
    const assistant = new MastraAgent({
      id: "assistant",
      name: "assistant",
      instructions: "Recall before answering.",
      model: model as never,
      tools: loomTools(agent, { userId: "alice" }, { only: ["loom_recall"] }),
    });
    expect((await assistant.generate("Which seat?")).text).toBe("Aisle.");
    expect(calls.map((c) => c.op)).toEqual(["memory.search"]);
    expect(calls[0].body).toMatchObject({ query: "seat", user_id: "alice" });
    expect(JSON.stringify(prompts.at(-1))).toContain("Alice prefers aisle");
  });

  it("hands a refused call's message to a Mastra agent's model, and the run goes on", async () => {
    const { agent, calls } = harness({});
    const { model, prompts } = recallThenAnswer({ query: "" });
    const assistant = new MastraAgent({
      id: "assistant",
      name: "assistant",
      instructions: "Recall before answering.",
      model: model as never,
      tools: loomTools(agent, {}, { only: ["loom_recall"] }),
    });
    expect((await assistant.generate("Which seat?")).text).toBe("Aisle.");
    expect(calls).toHaveLength(0);
    expect(JSON.stringify(prompts.at(-1))).toContain(
      '"type":"error-text","value":"tool argument query is required and must be a non-empty string"',
    );
  });

  it("hands a server error to a Mastra agent's model as a failed tool result", async () => {
    const { agent } = harness({ detail: "memory_key is too long" }, 422);
    const { model, prompts } = scripted([
      [
        {
          type: "tool-call",
          toolCallId: "c1",
          toolName: "loom_remember",
          input: JSON.stringify({ memory_key: "seat", value: "aisle" }),
        },
      ],
      [{ type: "text", text: "Saved." }],
    ]);
    const assistant = new MastraAgent({
      id: "assistant",
      name: "assistant",
      instructions: "Remember what the user says.",
      model: model as never,
      tools: loomTools(agent, {}, { only: ["loom_remember"] }),
    });
    expect((await assistant.generate("I like aisle seats")).text).toBe("Saved.");
    expect(JSON.stringify(prompts.at(-1))).toMatch(/"type":"error-text","value":"[^"]*memory_key is too long/);
  });

  it("lets Mastra refuse a call outside Loom's schema with its own message, and the run goes on", async () => {
    const { agent, calls } = harness({});
    const { model, prompts } = recallThenAnswer({ query: "seat", top_k: 50 });
    const assistant = new MastraAgent({
      id: "assistant",
      name: "assistant",
      instructions: "Recall before answering.",
      model: model as never,
      tools: loomTools(agent, {}, { only: ["loom_recall"] }),
    });
    expect((await assistant.generate("Which seat?")).text).toBe("Aisle.");
    expect(calls).toHaveLength(0);
    expect(JSON.stringify(prompts.at(-1))).toContain("Tool input validation failed for loom_recall");
  });
});
