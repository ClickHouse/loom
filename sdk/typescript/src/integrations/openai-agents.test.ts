import type { AgentInputItem } from "@openai/agents";
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

/** A Loom server keeping memory rows by (user, session, key), listed as the server lists
 * them: the newest second first, by key within a second, values cut to 2,048 characters. */
function rowsHarness(unserved = 0, cap = 100) {
  const rows = new Map<string, { key: string; value: unknown; user: unknown; session: unknown; second: number }>();
  const hidden = new Set<string>();
  const clock = { second: 0 };
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
        second: clock.second,
      });
    } else if (op === "memory.list") {
      const mine = [...rows.values()]
        .filter((r) => r.user === body.user_id && r.session === body.session_id && r.key.startsWith(body.prefix))
        .sort((a, b) => b.second - a.second || (a.key < b.key ? -1 : 1));
      const scanned: Array<(typeof mine)[number] | null> = [...Array.from({ length: unserved }, () => null), ...mine];
      const effectiveLimit = Math.min(cap, body.limit);
      const page = scanned.slice(body.offset, body.offset + effectiveLimit).filter((r) => r !== null);
      reply = {
        entries: page.map((r) => {
          const text = JSON.stringify(r.value);
          const cut = text.length > 2048;
          return {
            memory_key: r.key,
            value: hidden.has(r.key) ? null : cut ? `${text.slice(0, 2048)}…` : r.value,
            value_truncated: cut || hidden.has(r.key),
            created_at: `2026-01-01T00:00:${String(r.second).padStart(2, "0")}`,
          };
        }),
        truncated: body.offset + effectiveLimit < scanned.length,
      };
    } else if (op === "memory.get") {
      const row = rows.get(id(body.memory_key));
      reply = row ? { found: true, value: row.value } : { found: false };
    } else if (op === "memory.delete") {
      rows.delete(id(body.memory_key));
    }
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  return {
    agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }),
    rows,
    calls,
    clock,
    hidden,
  };
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
    expect(String(sets[0].session_id)).toMatch(/^oai-ts-[0-9a-f]{20}$/);
  });

  it("separates native TypeScript history from Python history", async () => {
    const { agent, calls } = rowsHarness();
    await new LoomSession(agent, "s1", { userId: "alice" }).addItems([msg("user", "x")]);
    // The digest matches Python, but its oai-py- session stores a different item format.
    expect(calls[0].body.session_id).toBe("oai-ts-65872b142b2cfb23bddc");
  });

  it("keeps native tool history separate from Python tool results", async () => {
    const { agent, calls } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    const items: AgentInputItem[] = [
      { type: "function_call", callId: "c1", name: "recall", arguments: "{}" },
      { type: "function_call_result", callId: "c1", name: "recall", output: "remembered", status: "completed" },
    ];
    await session.addItems(items);
    await agent.call(
      "memory.set",
      {
        memory_key: String(calls[0].body.memory_key),
        value: JSON.stringify({ type: "function_call_output", call_id: "c1", output: "other" }),
        memory_type: "episodic",
      },
      { userId: "alice", sessionId: "oai-py-65872b142b2cfb23bddc" },
    );
    expect(await session.getItems()).toEqual(items);
  });

  it.each([0, -1])("returns no items for limit %s with paginated history", async (limit) => {
    const { agent } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    await session.addItems(Array.from({ length: 101 }, (_, i) => msg("user", `m${i}`)));
    expect(await session.getItems(limit)).toEqual([]);
  });

  it("advances past an empty page of unserved projections", async () => {
    const { agent, calls } = rowsHarness(100, 50);
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    const items = [msg("user", "hi"), msg("assistant", "hello")];
    await session.addItems(items);
    expect(await session.getItems()).toEqual(items);
    const offsets = calls.filter((c) => c.op === "memory.list").map((c) => Number(c.body.offset));
    expect(offsets).toEqual([...new Set(offsets)].sort((a, b) => a - b));
  });

  it("keeps every item once when the server caps a viewer page", async () => {
    const { agent } = rowsHarness(0, 50);
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    const items = Array.from({ length: 120 }, (_, i) => msg("user", `m${i}`));
    await session.addItems(items);
    expect(await session.getItems()).toEqual(items);
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

  it("returns a history spanning seconds in the order it was added, and stops a latest read early", async () => {
    const { agent, clock, calls } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    for (const [second, pair] of [
      ["u1", "a1"],
      ["u2", "a2"],
      ["u3", "a3"],
    ].entries()) {
      clock.second = second;
      await session.addItems([msg("user", pair[0]), msg("assistant", pair[1])]);
    }
    const contents = (items: unknown[]) => items.map((i) => (i as { content: string }).content);
    expect(contents(await session.getItems())).toEqual(["u1", "a1", "u2", "a2", "u3", "a3"]);
    calls.length = 0;
    expect(contents(await session.getItems(1))).toEqual(["a3"]);
    expect(calls.filter((c) => c.op === "memory.list")).toHaveLength(1);
    expect(await session.popItem()).toEqual(msg("assistant", "a3"));
  });

  it("reads a long or sensitive item whole by key", async () => {
    const { agent, rows, hidden } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    const long = msg("assistant", "x".repeat(5000));
    await session.addItems([msg("user", "my key is sk-123"), long]);
    for (const r of rows.values()) if ((r.value as { content: string }).content.startsWith("my key")) hidden.add(r.key);
    expect(await session.getItems()).toEqual([msg("user", "my key is sk-123"), long]);
  });

  it("does not read a nested key prefix's items as its own", async () => {
    const { agent } = rowsHarness();
    const outer = new LoomSession(agent, "s1", { keyPrefix: "a" });
    const inner = new LoomSession(agent, "s1", { keyPrefix: "a/b" });
    await inner.addItems([msg("user", "inner")]);
    expect(await outer.getItems()).toEqual([]);
  });

  it("keeps non-ASCII user ids distinct", async () => {
    const { agent, calls } = rowsHarness();
    await new LoomSession(agent, "s1", { userId: "jos\u00e9" }).addItems([msg("user", "x")]);
    expect(calls[0].body.session_id).toBe("oai-ts-c10f2e9c4ca65c3f8b98");
  });

  it("refuses a missing id, a user naming none and an empty prefix", () => {
    const { agent } = rowsHarness();
    expect(() => new LoomSession(agent, "")).toThrow("sessionId");
    expect(() => new LoomSession(agent, "s1", { userId: "-" })).toThrow("names none");
    expect(() => new LoomSession(agent, "s1", { keyPrefix: "/" })).toThrow("keyPrefix");
  });
});
