import Anthropic from "@anthropic-ai/sdk";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { loomMemoryTool } from "./claude-memory.js";

type RequestBody = {
  messages: Array<{ role: string; content: unknown }>;
  tools: Array<{ name: string; type: string }>;
};

function message(content: unknown[], stopReason = "end_turn"): Response {
  return new Response(
    JSON.stringify({
      id: "msg_memory_test",
      type: "message",
      role: "assistant",
      model: "claude-sonnet-4-5",
      content,
      stop_reason: stopReason,
      stop_sequence: null,
      usage: { input_tokens: 1, output_tokens: 1 },
    }),
    { headers: { "content-type": "application/json" } },
  );
}

function provider(commands: unknown[]) {
  const requests: RequestBody[] = [];
  let index = 0;
  const client = new Anthropic({
    apiKey: "example-key",
    maxRetries: 0,
    fetch: async (_url, init) => {
      requests.push(JSON.parse(String(init?.body)) as RequestBody);
      if (index % 2 === 0) {
        const input = commands[index / 2];
        if (input === undefined) throw new Error("Unexpected model request");
        index++;
        return message([{ type: "tool_use", id: `tool_${index}`, name: "memory", input }], "tool_use");
      }
      index++;
      return message([{ type: "text", text: "done" }]);
    },
  });
  return { client, requests };
}

function storage(refuse = false) {
  const documents = new Map<string, string>();
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const agent = new Agent("example-org", "example-namespace", "claude-memory-example", {
    baseUrl: "https://loom.example",
    apiKey: "example-token",
    logLevel: "off",
    retry: { maxAttempts: 1 },
    fetch: async (url, init) => {
      const op = url.split("/v1/")[1] ?? "";
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      calls.push({ op, body });
      if (refuse && op === "knowledge.upsert") {
        return new Response(JSON.stringify({ detail: "Not authorized" }), { status: 403 });
      }
      const uri = String(body.source_uri);
      if (op === "knowledge.upsert") {
        documents.set(uri, String(body.content));
        return Response.json({});
      }
      if (op === "knowledge.get") {
        return Response.json({ found: documents.has(uri), source_uri: uri, content: documents.get(uri) });
      }
      if (op === "knowledge.list") return Response.json({ rows: [], next_cursor: null });
      throw new Error("Unexpected storage operation");
    },
  });
  return { agent, calls, documents };
}

function run(client: Anthropic, agent: Agent, prompt: string) {
  return client.beta.messages
    .toolRunner({
      model: "claude-sonnet-4-5",
      max_tokens: 64,
      max_iterations: 2,
      tools: [loomMemoryTool(agent, { store: "example-store" })],
      messages: [{ role: "user", content: prompt }],
    })
    .runUntilDone();
}

function result(request: RequestBody | undefined): { is_error?: boolean; content?: unknown } {
  const last = request?.messages.at(-1)?.content;
  if (!Array.isArray(last) || last.length !== 1) throw new Error("Expected one native tool result");
  return last[0] as { is_error?: boolean; content?: unknown };
}

describe("Claude memory native tool runner", () => {
  it("uses the native memory definition and shares stored files across fresh histories", async () => {
    const { client, requests } = provider([
      {
        command: "create",
        path: "/memories/preferences.md",
        file_text: "I prefer tea.",
        org: "other-org",
        namespace: "other-namespace",
      },
      { command: "view", path: "/memories/preferences.md" },
    ]);
    const { agent, calls, documents } = storage();
    await run(client, agent, "Remember my preference.");
    await run(client, agent, "Read the stored preference.");
    expect(requests[0]?.tools).toEqual([{ name: "memory", type: "memory_20250818" }]);
    expect(requests[0]?.messages).toEqual([{ role: "user", content: "Remember my preference." }]);
    expect(requests[2]?.messages).toEqual([{ role: "user", content: "Read the stored preference." }]);
    expect(result(requests[1]).is_error).not.toBe(true);
    expect(result(requests[3]).content).toContain("I prefer tea.");
    expect(documents.get("loom-memory://example-store/memories/preferences.md")).toBe("I prefer tea.");
    expect(calls.length).toBeGreaterThan(0);
    for (const { body } of calls) {
      expect(body).toMatchObject({ org: "example-org", namespace: "example-namespace" });
    }
  });

  it("returns path refusal as a native tool error before storage I/O", async () => {
    const { client, requests } = provider([{ command: "view", path: "/memories/../../outside" }]);
    const { agent, calls } = storage();
    await run(client, agent, "Read an invalid path.");
    expect(result(requests[1]).is_error).toBe(true);
    expect(calls).toEqual([]);
  });

  it("does not acknowledge a denied storage write as success", async () => {
    const { client, requests } = provider([
      { command: "create", path: "/memories/preferences.md", file_text: "I prefer tea." },
    ]);
    const { agent, calls, documents } = storage(true);
    await run(client, agent, "Remember my preference.");
    expect(result(requests[1]).is_error).toBe(true);
    expect(calls.map(({ op }) => op)).toEqual(["knowledge.get", "knowledge.list", "knowledge.upsert"]);
    expect(documents.size).toBe(0);
  });
});
