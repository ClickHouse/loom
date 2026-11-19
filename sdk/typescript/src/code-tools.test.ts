/** Model code lookup stays within application scope and returns budgeted valid evidence. */
import { describe, expect, it } from "vitest";
import { Agent } from "./agent.js";
import { LoomClient } from "./client.js";
import { Transport } from "./transport.js";

function harness(data: unknown, status = 200) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ op: url.split("/v1/")[1], body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify(data), { status, headers: { "X-Request-Id": "code-result-123" } });
  };
  const agent = new Agent(
    "acme",
    "memory-home",
    "coder",
    new LoomClient(new Transport({ baseUrl: "http://x", fetch, logLevel: "off" })),
  );
  return { agent, calls };
}

describe("opt-in code tools", () => {
  it("preserves defaults and formats the explicit code tool in each provider shape", () => {
    const { agent, calls } = harness({});
    expect(agent.tools().definitions()).toHaveLength(6);
    expect(() => agent.tools({}, { only: ["loom_code_lookup"] })).toThrow(/requires codeNamespace/);
    const tools = agent.tools({}, { codeNamespace: "code-repo", only: ["loom_code_lookup"] });
    expect(tools.definitions()).toMatchObject([
      { name: "loom_code_lookup", parameters: { required: ["kind", "target"], additionalProperties: false } },
    ]);
    expect(tools.definitions("anthropic")[0]).toHaveProperty("input_schema");
    expect(tools.definitions("openai")[0].function.name).toBe("loom_code_lookup");
    expect(agent.tools({}, { codeNamespace: "code-repo" }).definitions()).toHaveLength(7);
    expect(agent.tools({}, { codeNamespace: "code-repo" }).definitions()[0].name).toBe("loom_code_lookup");
    expect(() => agent.tools({}, { codeNamespace: " " })).toThrow();
    expect(() => agent.tools({}, { codeNamespace: "code-repo", maxResultChars: 255 })).toThrow();
    expect(calls).toEqual([]);
  });

  it.each(["Foo::~Foo", "Foo::operator()", "src/Foo.cpp:123"])(
    "accepts common C++/path references %s at three caller hops",
    async (target) => {
      const { agent, calls } = harness({ edges: [], epistemic: "lower_bound" });
      await agent
        .tools({}, { codeNamespace: "code-repo" })
        .execute("loom_code_lookup", { kind: "callers", target, depth: 3 });
      expect(calls[0].body).toMatchObject({ name: target, depth: 3 });
    },
  );

  it("preserves scope on authorization refusal without trying another namespace", async () => {
    const { agent, calls } = harness({ detail: { code: "FORBIDDEN", message: "denied" } }, 403);
    await expect(
      agent.tools({}, { codeNamespace: "code-repo" }).execute("loom_code_lookup", { kind: "symbol", target: "pkg.fn" }),
    ).rejects.toThrow();
    expect(calls).toHaveLength(1);
    expect(calls[0].body.namespace).toBe("code-repo");
    expect(agent.namespace).toBe("memory-home");
  });

  it.each([
    ["symbol", "src.flask.blueprints::Blueprint::__init__", undefined, { token_budget: 2500 }],
    ["search", "How are nested blueprints registered?", undefined, { top_k: 3, token_budget: 2500 }],
    ["callers", "DB::Context::getSettingsRef", 2, { depth: 2, max_nodes: 20 }],
    ["impact", "std::vector<int>::push_back", 2, { depth: 2, per_hop_limit: 20 }],
  ])(
    "dispatches %s via actual metadata transport without changing memory scope",
    async (kind, target, depth, fields) => {
      const data = {
        ambiguous: true,
        candidates: ["pkg.Other"],
        epistemic: "lower_bound",
        causes: ["treesitter_only_files"],
        as_of: { git_head: "abc123" },
      };
      const { agent, calls } = harness(data);
      const tools = agent.tools({ userId: "u", sessionId: "s" }, { codeNamespace: "code-repo" });
      const out = JSON.parse(await tools.execute("loom_code_lookup", { kind, target, ...(depth ? { depth } : {}) }));
      expect(out).toEqual({
        op: `code.${kind}`,
        request_id: "code-result-123",
        scope: { org: "acme", namespace: "code-repo" },
        data,
      });
      expect(calls[0]).toMatchObject({
        op: `code.${kind}`,
        body: { org: "acme", namespace: "code-repo", agent: "coder", user_id: "u", include_lessons: false, ...fields },
      });
      expect(calls[0].body[kind === "search" ? "query" : "name"]).toBe(target);
      if (kind === "symbol")
        expect(calls[0].body).toMatchObject({
          include_history: false,
          include_examples: false,
          uses: false,
          uses_brief: false,
        });
      await tools.execute("loom_note", { addition: "keep this in memory" });
      expect(calls.at(-1)?.body.namespace).toBe("memory-home");
    },
  );

  it.each([
    { org: "other" },
    { namespace: "other" },
    { op: "memory.set" },
    { token_budget: 9999 },
    { lesson_namespaces: ["private"] },
  ])("rejects model scope/op/budget injection %j before network", async (extra) => {
    const { agent, calls } = harness({});
    await expect(
      agent
        .tools({}, { codeNamespace: "code-repo" })
        .execute("loom_code_lookup", { kind: "symbol", target: "pkg.fn", ...extra }),
    ).rejects.toThrow(/unexpected/);
    expect(calls).toEqual([]);
  });

  it.each([
    { kind: "callers", target: "who calls this function" },
    { kind: "symbol", target: "where is this function" },
    { kind: "impact", target: "what might break" },
    { kind: "impact", target: "pkg.fn", depth: 3 },
    { kind: "symbol", target: "pkg.fn", depth: 2 },
    { kind: "callers", target: "pkg.fn", depth: 4 },
  ])("rejects invalid references/depth %j before network", async (args) => {
    const { agent, calls } = harness({});
    await expect(agent.tools({}, { codeNamespace: "code-repo" }).execute("loom_code_lookup", args)).rejects.toThrow();
    expect(calls).toEqual([]);
  });

  it("elides whole bodies honestly and leaves larger configured budgets intact", async () => {
    const unit = {
      path: "src/code.py",
      start_line: 5,
      end_line: 90,
      body: `def work():\n${"    pass\n".repeat(300)}`,
      elided: false,
      tier: "treesitter",
    };
    const data = {
      definitions: [unit],
      ambiguous: false,
      as_of: { git_head: "abc123" },
      epistemic: "lower_bound",
      causes: ["treesitter_only_files"],
    };
    const { agent } = harness(data);
    const text = await agent
      .tools({}, { codeNamespace: "code-repo", maxResultChars: 900 })
      .execute("loom_code_lookup", { kind: "symbol", target: "pkg.fn" });
    const out = JSON.parse(text);
    expect(text.length).toBeLessThanOrEqual(900);
    expect(out.data).toEqual({ ...data, definitions: [{ ...unit, body: "", elided: true }] });
    expect(out.request_id).toBe("code-result-123");
    expect(out.client_truncation).toMatchObject({ omitted_bodies: 1, insufficient: true });
    const full = JSON.parse(
      await agent
        .tools({}, { codeNamespace: "code-repo", maxResultChars: 12000 })
        .execute("loom_code_lookup", { kind: "symbol", target: "pkg.fn" }),
    );
    expect(full.data).toEqual(data);
    expect(full).not.toHaveProperty("client_truncation");
  });

  it("elides whole rows and returns explicit insufficiency when metadata cannot fit", async () => {
    const edge = { path: "src/code.py", line: 9, tier: "treesitter", confidence: 0.8, from_symbol: "p".repeat(180) };
    const { agent } = harness({
      edges: Array(20).fill(edge),
      counts_floor: true,
      epistemic: "lower_bound",
      causes: ["truncated"],
      truncated: true,
    });
    const text = await agent
      .tools({}, { codeNamespace: "code-repo", maxResultChars: 900 })
      .execute("loom_code_lookup", { kind: "callers", target: "pkg.fn" });
    const out = JSON.parse(text);
    expect(text.length).toBeLessThanOrEqual(900);
    expect(out.data.counts_floor).toBe(true);
    expect(out.data.edges.every((row: unknown) => JSON.stringify(row) === JSON.stringify(edge))).toBe(true);
    expect(out.data.edges.length + out.client_truncation.omitted_rows.edges).toBe(20);
    const other = harness({ note: "long uncertainty ".repeat(1000) }).agent;
    const small = await other
      .tools({}, { codeNamespace: "code-repo", maxResultChars: 256 })
      .execute("loom_code_lookup", { kind: "symbol", target: "pkg.fn" });
    expect(small.length).toBeLessThanOrEqual(256);
    expect(JSON.parse(small)).toMatchObject({ client_truncation: { insufficient: true, metadata_omitted: true } });
    expect(JSON.parse(small)).not.toHaveProperty("data");
  });
});
