import { generateText, stepCountIs } from "ai";
import { MockLanguageModelV4 } from "ai/test";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LOOM_TOOL_SPECS } from "../tools.js";
import { loomTools } from "./ai-sdk.js";

function harness(reply: unknown) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ op: url.split("/v1/")[1], body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
};

describe("loomTools (AI SDK adapter)", () => {
  it("keys every verb by name with Loom's description and JSON schema", async () => {
    const tools = loomTools(harness({}).agent);
    expect(Object.keys(tools)).toEqual(LOOM_TOOL_SPECS.map((s) => s.name));
    for (const spec of LOOM_TOOL_SPECS) {
      expect(tools[spec.name].description).toBe(spec.description);
      expect(await (tools[spec.name].inputSchema as { jsonSchema: unknown }).jsonSchema).toEqual(spec.parameters);
    }
  });

  it("keeps a subset with only", () => {
    expect(Object.keys(loomTools(harness({}).agent, {}, { only: ["loom_recall"] }))).toEqual(["loom_recall"]);
  });

  it("runs a model's loom_recall call through memory.search in the bound scope", async () => {
    const { agent, calls } = harness({
      results: [{ memory_key: "seat", content_excerpt: "Alice prefers aisle", similarity: 0.91 }],
    });
    const model = new MockLanguageModelV4({
      doGenerate: {
        content: [
          {
            type: "tool-call",
            toolCallId: "c1",
            toolName: "loom_recall",
            input: JSON.stringify({ query: "seat preference", top_k: 3 }),
          },
        ],
        finishReason: { unified: "tool-calls", raw: undefined },
        usage,
        warnings: [],
      } as never,
    });
    const result = await generateText({ model, tools: loomTools(agent, { userId: "alice" }), prompt: "seat?" });
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("memory.search");
    expect(calls[0].body).toMatchObject({ query: "seat preference", top_k: 3, user_id: "alice" });
    const output = result.toolResults[0]?.output as string;
    expect(JSON.parse(output)).toEqual({
      memories: [{ memory_key: "seat", content: "Alice prefers aisle", similarity: 0.91 }],
    });
  });

  it("hands a refused call's Loom message to the model as a tool error, and the run goes on", async () => {
    const { agent, calls } = harness({});
    const prompts: unknown[] = [];
    let step = 0;
    const model = new MockLanguageModelV4({
      doGenerate: async (options: { prompt: unknown }) => {
        prompts.push(options.prompt);
        const first = step++ === 0;
        return {
          content: first
            ? [{ type: "tool-call", toolCallId: "c1", toolName: "loom_recall", input: JSON.stringify({ query: "" }) }]
            : [{ type: "text", text: "Which seat?" }],
          finishReason: { unified: first ? "tool-calls" : "stop", raw: undefined },
          usage,
          warnings: [],
        };
      },
    } as never);
    const result = await generateText({ model, tools: loomTools(agent), prompt: "seat?", stopWhen: stepCountIs(2) });
    expect(result.text).toBe("Which seat?");
    expect(calls).toHaveLength(0);
    expect(JSON.stringify(prompts.at(-1))).toContain(
      '"type":"error-text","value":"LoomError: tool argument query is required and must be a non-empty string"',
    );
  });
});
