import { describe, expect, it, vi } from "vitest";
import { Agent, Datapack, SENTINEL } from "./agent.js";
import { LoomClient } from "./client.js";
import { GENERATED_OP_COUNT, OP_BY_NAME, OPS } from "./generated/ops.js";
import { Transport } from "./transport.js";

function client() {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    calls.push({ url, body: JSON.parse(init.body as string) });
    return new Response(JSON.stringify({ results: [], ok: true }), { status: 200 });
  };
  const c = new LoomClient(new Transport({ baseUrl: "http://x", fetch }));
  return { c, calls };
}

describe("generated table", () => {
  it("covers every op with a unique name, a group and a camelCase method", () => {
    expect(OPS.length).toBe(GENERATED_OP_COUNT);
    expect(OPS.length).toBeGreaterThan(200);
    expect(new Set(OPS.map((o) => o.name)).size).toBe(OPS.length);
    for (const o of OPS) {
      expect(o.name.startsWith(`${o.group}.`)).toBe(true);
      expect(o.method).toMatch(/^[a-z][A-Za-z0-9]*$/);
    }
    expect(OP_BY_NAME["memory.search"].idempotent).toBe(true);
    expect(OP_BY_NAME["memory.set_from_messages"].idempotent).toBe(false);
    expect(OP_BY_NAME["memory.bulk_set"].method).toBe("bulkSet");
  });
});

describe("LoomClient", () => {
  it("routes call() and the group accessors to the dotted op path", async () => {
    const { c, calls } = client();
    await c.call("memory.search", { org: "o", namespace: "n", query: "q" });
    await c.memory.search({ org: "o", namespace: "n", query: "q2" });
    await c.knowledge.bulkAdd({ org: "o", namespace: "n", entries: [{ content: "x", source_uri: "u" }] });
    expect(calls.map((x) => x.url)).toEqual([
      "http://x/v1/memory.search",
      "http://x/v1/memory.search",
      "http://x/v1/knowledge.bulk_add",
    ]);
    expect(calls[1].body.query).toBe("q2");
  });

  it("passes a read's version_text to memory.delete_version exactly", async () => {
    // A nanosecond version is above 2^53: JSON.parse rounds the number, the string is exact.
    const exact = "1792436295126090001";
    const sent: Array<Record<string, unknown>> = [];
    const fetch = async (url: string, init: RequestInit) => {
      sent.push(JSON.parse(init.body as string));
      const body = url.endsWith("/memory.get")
        ? `{"found":true,"memory_key":"k","version":${exact},"version_text":"${exact}"}`
        : `{"tombstoned":true,"version":1}`;
      return new Response(body, { status: 200 });
    };
    const c = new LoomClient(new Transport({ baseUrl: "http://x", fetch }));
    const ident = { org: "o", namespace: "n", agent: "a", memory_key: "k" };
    const got = await c.call("memory.get", ident);
    expect(String(got.version)).not.toBe(exact);
    await c.call("memory.delete_version", { ...ident, version: got.version_text ?? "" });
    expect(sent[1].version).toBe(exact);
  });

  it("exposes every generated group as a property", () => {
    const { c } = client();
    for (const g of new Set(OPS.map((o) => o.group))) {
      expect(typeof (c as unknown as Record<string, unknown>)[g]).toBe("object");
    }
  });

  it("defaults the base URL to LOOM_BASE_URL, else localhost", () => {
    vi.stubEnv("LOOM_BASE_URL", "");
    expect(new LoomClient({ baseUrl: "" }).transport.baseUrl).toBe("http://localhost:7777");
    expect(new LoomClient().transport.baseUrl).toBe("http://localhost:7777");
    vi.stubEnv("LOOM_BASE_URL", " http://env.test/ ");
    expect(new LoomClient({}).transport.baseUrl).toBe("http://env.test");
    vi.unstubAllEnvs();
  });
});

describe("Agent / Datapack", () => {
  it("stamps the identity tuple with sentinels and lets the body win", async () => {
    const { c, calls } = client();
    const bot = new Datapack("acme", "support", c).agent("assistant");
    await bot.recall("seat", { userId: "u1" });
    expect(calls[0].body).toMatchObject({
      org: "acme",
      namespace: "support",
      agent: "*",
      user_id: "u1",
      session_id: SENTINEL,
      run_id: SENTINEL,
      query: "seat",
    });
    await bot.remember("k", "v", { sessionId: "s9", agentId: "orchestrated", tags: ["t"] });
    expect(calls[1].url).toBe("http://x/v1/memory.set");
    expect(calls[1].body).toMatchObject({
      agent: "orchestrated",
      session_id: "s9",
      memory_key: "k",
      value: "v",
      tags: ["t"],
    });
    await bot.ask("why?", { userId: "u1", top_k: 3 } as never);
    expect(calls[2].url).toBe("http://x/v1/memory.ask");
    expect(calls[2].body).toMatchObject({ query: "why?", top_k: 3, user_id: "u1", agent: "*" });
    await bot.ingest([{ role: "user", content: "hi" }] as never, { sessionId: "s1" });
    expect(calls[3].url).toBe("http://x/v1/memory.set_from_messages");
    await bot.outcome("k", "used" as never);
    expect(calls[4].url).toBe("http://x/v1/memory.outcome");
    await bot.contextPack({ token_budget: 800 } as never, { sessionId: "s1" });
    expect(calls[5].body).toMatchObject({ token_budget: 800, session_id: "s1" });
    await bot.recall("seat", { agentId: "assistant" });
    expect(calls[6].body).toMatchObject({ agent: "assistant" });
  });

  it("an Agent built from options owns its client", () => {
    const a = new Agent("o", "n", "a", { baseUrl: "http://x", fetch: async () => new Response("{}") });
    expect(a.client).toBeInstanceOf(LoomClient);
    expect(a.identity()).toEqual({ org: "o", namespace: "n", agent: "a", user_id: "-", session_id: "-", run_id: "-" });
  });
});

describe("Agent consumer", () => {
  function agentWith(consumer?: string) {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const fetch = async (url: string, init: RequestInit) => {
      calls.push({ url, body: JSON.parse(init.body as string) });
      return new Response(JSON.stringify({ results: [] }), { status: 200 });
    };
    const opts = consumer === undefined ? { baseUrl: "http://x", fetch } : { baseUrl: "http://x", fetch, consumer };
    return { a: new Agent("o", "n", "bot", opts), calls };
  }

  it("stamps the constructor consumer on every op body the agent posts", async () => {
    const { a, calls } = agentWith("support-bot");
    expect(a.consumer).toBe("support-bot");
    await a.recall("q");
    await a.remember("k", "v");
    await a.call("run.begin", { goal: "g" });
    await a.call("code.grep", { pattern: "x", consumer: "" } as never);
    expect(calls.map((c) => c.body.consumer)).toEqual(["support-bot", "support-bot", "support-bot", "support-bot"]);
    await a.call("memory.search", { query: "q", consumer: "other" } as never);
    expect(calls[4].body.consumer).toBe("other");
  });

  it("changes nothing for an agent that names no consumer", async () => {
    const { a, calls } = agentWith();
    expect(a.consumer).toBe("");
    await a.recall("q");
    expect("consumer" in calls[0].body).toBe(false);
    expect(
      new Datapack("o", "n", { baseUrl: "http://x", fetch: async () => new Response("{}") }).agent("b").consumer,
    ).toBe("");
  });
});

describe("Agent.mentor", () => {
  it("asks mentor.brief for this agent, logs up to three hints at info and returns the brief", async () => {
    const lines: string[] = [];
    const hint = (i: number) => ({ code: `c${i}`, severity: "info", text: `hint ${i}` });
    const brief = { org: "o", namespace: "n", agent: "bot", window_seconds: 60, computed_at: "", sessions: 1 };
    const bodies: Array<Record<string, unknown>> = [];
    const fetch = async (_url: string, init: RequestInit) => {
      bodies.push(JSON.parse(init.body as string));
      return new Response(JSON.stringify({ ...brief, hints: [1, 2, 3, 4].map(hint) }), { status: 200 });
    };
    const logger = { debug() {}, info: (m: string) => lines.push(m), warn() {}, error() {} };
    const bot = new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logger, logLevel: "info" });
    const out = await bot.mentor({ windowSeconds: 60 });
    expect(bodies[0]).toMatchObject({ org: "o", namespace: "n", agent: "bot", window_seconds: 60 });
    expect(out.hints?.length).toBe(4);
    expect(lines.filter((l) => l.startsWith("loom mentor"))).toEqual([
      "loom mentor (c1, info): hint 1",
      "loom mentor (c2, info): hint 2",
      "loom mentor (c3, info): hint 3",
    ]);
  });
});

describe("Agent.guide", () => {
  it("posts mentor.guide with the part and returns the text", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const fetch = async (url: string, init: RequestInit) => {
      calls.push([url, JSON.parse(init.body as string)]);
      return new Response(JSON.stringify({ version: "1", part: "reference", text: "# loom" }), { status: 200 });
    };
    const bot = new Agent("o", "n", "bot", { baseUrl: "http://x", fetch });
    expect(await bot.guide("reference")).toBe("# loom");
    expect(calls[0]?.[0]).toBe("http://x/v1/mentor.guide");
    expect(calls[0]?.[1]).toMatchObject({ part: "reference" });
  });
});
