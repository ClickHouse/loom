import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LOOM_TOOL_SPECS } from "../tools.js";
import { loomTools } from "./openai-agents.js";

function harness(reply: unknown, status = 200) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ op: url.split("/v1/")[1], body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify(reply), { status });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

describe("loomTools (OpenAI Agents SDK adapter)", () => {
  it("returns every verb as a non-strict function tool with Loom's schema", () => {
    const tools = loomTools(harness({}).agent);
    expect(tools.map((t) => t.name)).toEqual(LOOM_TOOL_SPECS.map((s) => s.name));
    for (const [i, t] of tools.entries()) {
      expect(t.type).toBe("function");
      expect(t.strict).toBe(false);
      expect(t.description).toBe(LOOM_TOOL_SPECS[i].description);
      expect(t.parameters).toEqual(LOOM_TOOL_SPECS[i].parameters);
    }
  });

  it("keeps a subset with only", () => {
    const tools = loomTools(harness({}).agent, {}, { only: ["loom_recall"] });
    expect(tools.map((t) => t.name)).toEqual(["loom_recall"]);
  });

  it("runs memory.search in the bound scope and returns the string the model reads", async () => {
    const { agent, calls } = harness({
      results: [{ memory_key: "seat", content_excerpt: "Alice prefers aisle", similarity: 0.91 }],
    });
    const [recall] = loomTools(agent, { userId: "alice" }, { only: ["loom_recall"] });
    const out = await recall.invoke({} as never, JSON.stringify({ query: "seat preference", top_k: 3 }));
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("memory.search");
    expect(calls[0].body).toMatchObject({ query: "seat preference", top_k: 3, user_id: "alice" });
    expect(JSON.parse(out as string)).toEqual({
      memories: [{ memory_key: "seat", content: "Alice prefers aisle", similarity: 0.91 }],
    });
  });

  it("returns a refused call's error to the model", async () => {
    const { agent, calls } = harness({});
    const [recall] = loomTools(agent, {}, { only: ["loom_recall"] });
    const out = (await recall.invoke({} as never, JSON.stringify({ query: "" }))) as string;
    expect(calls).toHaveLength(0);
    expect(out.startsWith("loom_recall failed: ")).toBe(true);
    expect(out).toContain("query");
  });

  it("returns a server error to the model", async () => {
    const { agent } = harness({ detail: "memory_key is too long" }, 422);
    const [remember] = loomTools(agent, {}, { only: ["loom_remember"] });
    const out = (await remember.invoke({} as never, JSON.stringify({ memory_key: "seat", value: "aisle" }))) as string;
    expect(out.startsWith("loom_remember failed: ")).toBe(true);
  });
});
