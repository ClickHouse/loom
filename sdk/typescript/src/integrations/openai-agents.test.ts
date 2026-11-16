import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LOOM_TOOL_SPECS } from "../tools.js";
import { LoomSession, loomTools } from "./openai-agents.js";

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

/** A loom server keeping memory rows by (user, session, key), listed by key two a page. */
function rowsHarness() {
  const rows = new Map<string, { key: string; value: unknown; user: unknown; session: unknown }>();
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string);
    calls.push({ op, body });
    const id = (key: string) => JSON.stringify([body.user_id, body.session_id, key]);
    let reply: unknown = {};
    if (op === "memory.set") {
      rows.set(id(body.memory_key), {
        key: body.memory_key,
        value: JSON.parse(body.value),
        user: body.user_id,
        session: body.session_id,
      });
    } else if (op === "memory.list") {
      const mine = [...rows.values()]
        .filter((r) => r.user === body.user_id && r.session === body.session_id && r.key.startsWith(body.prefix))
        .sort((a, b) => (a.key < b.key ? -1 : 1));
      const page = mine.slice(body.offset, body.offset + 2);
      reply = {
        entries: page.map((r) => ({ memory_key: r.key, value: r.value })),
        truncated: body.offset + 2 < mine.length,
      };
    } else if (op === "memory.delete") {
      rows.delete(id(body.memory_key));
    }
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), rows, calls };
}

const msg = (role: "user" | "assistant", content: string) => ({ role, content }) as never;

describe("LoomSession (OpenAI Agents SDK session)", () => {
  it("keeps items in order across pages, with repeats, as episodic rows of the user's loom session", async () => {
    const { agent, calls } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    expect(await session.getSessionId()).toBe("s1");
    await session.addItems([msg("user", "hi"), msg("assistant", "hello"), msg("user", "hi")]);
    await session.addItems([msg("assistant", "again")]);
    expect(await session.getItems()).toEqual([
      msg("user", "hi"),
      msg("assistant", "hello"),
      msg("user", "hi"),
      msg("assistant", "again"),
    ]);
    expect(await session.getItems(2)).toEqual([msg("user", "hi"), msg("assistant", "again")]);
    const sets = calls.filter((c) => c.op === "memory.set").map((c) => c.body);
    expect(new Set(sets.map((b) => b.memory_type))).toEqual(new Set(["episodic"]));
    expect(new Set(sets.map((b) => b.user_id))).toEqual(new Set(["alice"]));
    expect(String(sets[0].session_id)).toMatch(/^oai-[0-9a-f]{20}$/);
  });

  it("names the same loom session as the Python LoomSession", async () => {
    const { agent, calls } = rowsHarness();
    await new LoomSession(agent, "s1", { userId: "alice" }).addItems([msg("user", "x")]);
    // sha256 of the compact JSON pair ["alice","s1"], which the Python LoomSession hashes too.
    expect(calls[0].body.session_id).toBe("oai-65872b142b2cfb23bddc");
  });

  it("pops the latest item and clears only its own session", async () => {
    const { agent } = rowsHarness();
    const mine = new LoomSession(agent, "s1", { userId: "alice" });
    const other = new LoomSession(agent, "s2", { userId: "alice" });
    await mine.addItems([msg("user", "a"), msg("assistant", "b")]);
    await other.addItems([msg("user", "c")]);
    expect(await mine.popItem()).toEqual(msg("assistant", "b"));
    expect(await mine.getItems()).toEqual([msg("user", "a")]);
    await mine.clearSession();
    expect(await mine.getItems()).toEqual([]);
    expect(await mine.popItem()).toBeUndefined();
    expect(await other.getItems()).toEqual([msg("user", "c")]);
  });

  it("refuses a missing id, a user naming none and an empty prefix", () => {
    const { agent } = rowsHarness();
    expect(() => new LoomSession(agent, "")).toThrow("sessionId");
    expect(() => new LoomSession(agent, "s1", { userId: "-" })).toThrow("names none");
    expect(() => new LoomSession(agent, "s1", { keyPrefix: "/" })).toThrow("keyPrefix");
  });
});
