import type { AgentInputItem } from "@openai/agents";
import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LoomClient } from "../client.js";
import { AutomaticCaptureSuppressed, SessionProtection } from "../session-protection.js";
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
const appendCapabilities = {
  ops: [{ name: "memory.append_batch", required_role: "writer", request_fields: ["entries"] }],
};

function rowsHarness(unserved = 0, cap = 100) {
  const rows = new Map<string, { key: string; value: unknown; user: unknown; session: unknown; second: number }>();
  const hidden = new Set<string>();
  const disappear = new Set<string>();
  const clock = { second: 0 };
  const behavior = { serveAppend: true, discoveryStatus: 200, appendStatus: 200, appendCode: "", blockedContent: "" };
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string);
    calls.push({ op, body });
    const id = (key: string) => JSON.stringify([body.user_id, body.session_id, key]);
    let reply: unknown = {};
    if (op === "ops.capabilities") {
      return new Response(JSON.stringify(behavior.serveAppend ? appendCapabilities : { ops: [] }), {
        status: behavior.discoveryStatus,
      });
    }
    if (op === "memory.append_batch") {
      const entries = body.entries as Array<{ memory_key: string; value: string }>;
      const staged = entries.map((entry) => ({
        key: entry.memory_key,
        value: JSON.parse(entry.value),
        user: body.user_id,
        session: body.session_id,
        second: clock.second,
      }));
      if (
        (behavior.blockedContent && staged.some((row) => row.value.content === behavior.blockedContent)) ||
        (behavior.appendStatus !== 200 && behavior.appendCode !== "PUBLICATION_PENDING")
      ) {
        return new Response(JSON.stringify({ detail: { code: behavior.appendCode, message: "batch refused" } }), {
          status: behavior.appendStatus === 200 ? 422 : behavior.appendStatus,
        });
      }
      for (const row of staged) rows.set(id(row.key), row);
      return new Response(
        JSON.stringify(
          behavior.appendCode === "PUBLICATION_PENDING"
            ? { detail: { code: "PUBLICATION_PENDING", message: "committed; publication pending", committed: true } }
            : { written: staged.map((row) => ({ memory_key: row.key, version: 1 })) },
        ),
        { status: behavior.appendStatus },
      );
    }
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
      if (disappear.has(body.memory_key)) rows.delete(id(body.memory_key));
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
    behavior,
    hidden,
    disappear,
  };
}

const msg = (role: "user" | "assistant", content: string) => ({ role, content }) as never;

function historyFamily(beforeCommit: () => Promise<void> = async () => {}) {
  const state = { value: "clean" as "clean" | "protected", marks: 0 };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.marks++;
      await beforeCommit();
      state.value = "protected";
    },
  });
  return { root, state };
}

const historyKey = "openai-agents/00000000000000000001-00000001";

function protectedHistoryHarness(
  root: SessionProtection,
  reply: (op: string, body: Record<string, unknown>) => unknown = () => ({ entries: [], truncated: false }),
  token?: () => Promise<string>,
) {
  const calls: Array<{ op: string; body: Record<string, unknown> }> = [];
  const client = new LoomClient({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    ...(token ? { tokenProvider: { origin: "http://inert.invalid", token } } : {}),
    fetch: async (url, init) => {
      const op = url.split("/v1/")[1];
      const body = JSON.parse(init.body as string) as Record<string, unknown>;
      calls.push({ op, body });
      return new Response(JSON.stringify(op === "ops.capabilities" ? appendCapabilities : reply(op, body)), {
        headers: { "content-type": "application/json" },
      });
    },
    logLevel: "off",
  });
  return { client, calls, agent: new Agent("o", "n", "bot", client) };
}

describe("LoomSession conversation protection", () => {
  it.each(["getItems", "addItems"] as const)("suppresses protected native %s before HTTP", async (method) => {
    const family = historyFamily();
    const { agent, calls } = protectedHistoryHarness(family.root);
    const session = new LoomSession(agent, "s1");
    family.state.value = "protected";
    const call = method === "getItems" ? session.getItems() : session.addItems([msg("user", "private")]);
    await expect(call).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls).toEqual([]);
  });

  it.each(["getItems", "addItems"] as const)(
    "retains the constructor family for native %s after rebinding",
    async (method) => {
      const first = historyFamily();
      const original = protectedHistoryHarness(first.root);
      const session = new LoomSession(original.agent, "s1");
      const current = protectedHistoryHarness(historyFamily().root);
      Object.defineProperty(original.agent, "client", { value: current.client });
      first.state.value = "protected";
      const call = method === "getItems" ? session.getItems() : session.addItems([msg("user", "private")]);
      await expect(call).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect([...original.calls, ...current.calls]).toEqual([]);
    },
  );

  it("acknowledges both retained and rebound families before returning marked history", async () => {
    let release: (() => void) | undefined;
    const commit = new Promise<void>((resolve) => {
      release = resolve;
    });
    const first = historyFamily(() => commit);
    const original = protectedHistoryHarness(first.root);
    const session = new LoomSession(original.agent, "s1");
    let entered: (() => void) | undefined;
    const observed = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const second = historyFamily(async () => {
      entered?.();
    });
    const current = protectedHistoryHarness(second.root, () => ({
      mirrored: true,
      entries: [{ memory_key: historyKey, value: msg("user", "private") }],
      truncated: false,
    }));
    Object.defineProperty(original.agent, "client", { value: current.client });
    let returned = false;
    const read = session.getItems().then((items) => {
      returned = true;
      return items;
    });
    try {
      await observed;
      expect(first.state.marks).toBe(1);
      expect(returned).toBe(false);
    } finally {
      release?.();
      await read;
    }
    expect(await read).toEqual([msg("user", "private")]);
    expect(first.state.value).toBe("protected");
    expect(second.state.value).toBe("protected");
  });

  it("observes a marked discovery response before any append", async () => {
    const family = historyFamily();
    const calls: string[] = [];
    const client = new LoomClient({
      baseUrl: "http://inert.invalid",
      sessionProtection: family.root,
      fetch: async (url) => {
        calls.push(url.split("/v1/")[1]);
        return new Response(JSON.stringify({ ...appendCapabilities, mirrored: true }));
      },
      logLevel: "off",
    });
    const session = new LoomSession(new Agent("o", "n", "bot", client), "s1");
    await expect(session.addItems([msg("user", "private")])).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls).toEqual(["ops.capabilities"]);
    expect(family.state.value).toBe("protected");
  });

  it("refuses a marked batch acknowledgement without starting another write", async () => {
    const family = historyFamily();
    const { agent, calls } = protectedHistoryHarness(family.root, () => ({ mirrored: true }));
    const session = new LoomSession(agent, "s1");
    await expect(session.addItems([msg("user", "first"), msg("assistant", "second")])).rejects.toBeInstanceOf(
      AutomaticCaptureSuppressed,
    );
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities", "memory.append_batch"]);
    expect(calls[1].body.entries).toHaveLength(2);
    expect(family.state.value).toBe("protected");
  });

  it("stops native history pagination after a marked page", async () => {
    const family = historyFamily();
    const { agent, calls } = protectedHistoryHarness(family.root, (_op, body) => ({
      mirrored: body.offset === 0,
      entries: [],
      truncated: body.offset === 0,
    }));
    await expect(new LoomSession(agent, "s1").getItems()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls.map((call) => call.op)).toEqual(["memory.list"]);
    expect(family.state.value).toBe("protected");
  });

  it("stops native history hydration after a marked truncated item", async () => {
    const family = historyFamily();
    const { agent, calls } = protectedHistoryHarness(family.root, (op) =>
      op === "memory.list"
        ? {
            mirrored: true,
            entries: [{ memory_key: historyKey, value: null, value_truncated: true }],
            truncated: false,
          }
        : { found: true, value: msg("user", "private") },
    );
    await expect(new LoomSession(agent, "s1").getItems()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls.map((call) => call.op)).toEqual(["memory.list"]);
    expect(family.state.value).toBe("protected");
  });

  it.each(["getItems", "addItems"] as const)(
    "rechecks the retained native %s family after credentials rebind it",
    async (method) => {
      const first = historyFamily();
      const current = protectedHistoryHarness(historyFamily().root);
      const original = protectedHistoryHarness(first.root, undefined, async () => {
        await first.root.mark();
        Object.defineProperty(original.agent, "client", { value: current.client });
        return "token";
      });
      const session = new LoomSession(original.agent, "s1");
      const call = method === "getItems" ? session.getItems() : session.addItems([msg("user", "private")]);
      await expect(call).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
      expect([...original.calls, ...current.calls]).toEqual([]);
    },
  );

  it.each(["popItem", "clearSession"] as const)(
    "keeps explicit native %s usable and observes the retained family",
    async (method) => {
      const first = historyFamily();
      const original = protectedHistoryHarness(first.root);
      const session = new LoomSession(original.agent, "s1");
      const second = historyFamily();
      const current = protectedHistoryHarness(second.root, (op) =>
        op === "memory.list"
          ? {
              mirrored: true,
              entries: [{ memory_key: historyKey, value: msg("user", "private") }],
              truncated: false,
            }
          : {},
      );
      Object.defineProperty(original.agent, "client", { value: current.client });
      first.state.value = "protected";
      const result = await session[method]();
      expect(result).toEqual(method === "popItem" ? msg("user", "private") : undefined);
      expect(current.calls.map((call) => call.op)).toEqual(["memory.list", "memory.delete"]);
      expect(first.state.marks).toBe(1);
      expect(second.state.marks).toBe(1);
    },
  );
});

describe("LoomSession (OpenAI Agents SDK session)", () => {
  it("does not discover or mutate for an empty append", async () => {
    const { agent, calls } = rowsHarness();
    await new LoomSession(agent, "s1").addItems([]);
    expect(calls).toEqual([]);
  });

  it("encodes every item before discovery so a malformed later item writes nothing", async () => {
    const { agent, calls, rows } = rowsHarness();
    const cyclic = { role: "user", content: "bad", providerData: {} };
    cyclic.providerData = cyclic;
    await expect(
      new LoomSession(agent, "s1").addItems([msg("user", "valid"), cyclic as AgentInputItem]),
    ).rejects.toThrow();
    expect(calls).toEqual([]);
    expect(rows.size).toBe(0);
  });

  it("refuses an oversized logical append without chunking or discovery", async () => {
    const { agent, calls, rows } = rowsHarness();
    await expect(
      new LoomSession(agent, "s1").addItems(Array.from({ length: 1001 }, () => msg("user", "same"))),
    ).rejects.toThrow("at most 1000");
    expect(calls).toEqual([]);
    expect(rows.size).toBe(0);
  });

  it("preserves existing history when the second entry makes the batch refuse", async () => {
    const { agent, calls, rows, behavior } = rowsHarness();
    const session = new LoomSession(agent, "s1");
    await session.addItems([msg("user", "old")]);
    const before = [...rows.values()];
    calls.length = 0;
    behavior.blockedContent = "blocked";
    await expect(session.addItems([msg("user", "valid"), msg("user", "blocked")])).rejects.toThrow("batch refused");
    expect([...rows.values()]).toEqual(before);
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities", "memory.append_batch"]);
    expect(await session.getItems()).toEqual([msg("user", "old")]);
  });

  it("refuses a missing capability without attempting a point or ordinary bulk write", async () => {
    const { agent, calls, rows, behavior } = rowsHarness();
    behavior.serveAppend = false;
    await expect(new LoomSession(agent, "s1").addItems([msg("user", "new")])).rejects.toThrow("memory.append_batch");
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities"]);
    expect(rows.size).toBe(0);
  });

  it("propagates failed discovery without writing history", async () => {
    const { agent, calls, rows, behavior } = rowsHarness();
    behavior.discoveryStatus = 403;
    await expect(new LoomSession(agent, "s1").addItems([msg("user", "new")])).rejects.toMatchObject({ status: 403 });
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities"]);
    expect(rows.size).toBe(0);
  });

  it.each([404, 422])("does not downgrade when the append target refuses with %s", async (status) => {
    const { agent, calls, rows, behavior } = rowsHarness();
    behavior.appendStatus = status;
    await expect(new LoomSession(agent, "s1").addItems([msg("user", "new")])).rejects.toMatchObject({ status });
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities", "memory.append_batch"]);
    expect(rows.size).toBe(0);
  });

  it("surfaces committed publication-pending without resending or deleting the append", async () => {
    const { agent, calls, rows, behavior } = rowsHarness();
    behavior.appendStatus = 503;
    behavior.appendCode = "PUBLICATION_PENDING";
    const session = new LoomSession(agent, "s1");
    await expect(session.addItems([msg("user", "a"), msg("user", "b")])).rejects.toMatchObject({
      code: "PUBLICATION_PENDING",
      detail: { committed: true },
    });
    expect(rows.size).toBe(2);
    expect(calls.map((call) => call.op)).toEqual(["ops.capabilities", "memory.append_batch"]);
    expect(await session.getItems()).toEqual([msg("user", "a"), msg("user", "b")]);
  });

  it.each(["before", "after"])(
    "keeps the validated user when caller options change %s the first write",
    async (when) => {
      const { agent, calls } = rowsHarness();
      const options = { userId: "alice" };
      const session = new LoomSession(agent, "s1", options);
      if (when === "after") await session.addItems([msg("user", "first")]);
      options.userId = when === "before" ? "-" : "bob";
      await session.addItems([msg("user", "second")]);
      expect(calls.filter((call) => call.op === "memory.append_batch").map((call) => call.body.user_id)).toEqual(
        when === "before" ? ["alice"] : ["alice", "alice"],
      );
      expect((await session.getItems()).length).toBe(when === "before" ? 1 : 2);
      const resumed = new LoomSession(agent, "s1", { userId: "alice" });
      expect(await resumed.getItems()).toEqual(await session.getItems());
    },
  );

  it.each(["getItems", "popItem"] as const)(
    "uses the same key-order tail for %s when server times differ",
    async (method) => {
      const { agent, clock } = rowsHarness();
      const session = new LoomSession(agent, "s1", { userId: "alice" });
      const items = Array.from({ length: 102 }, (_, i) => msg("user", `m${i}`));
      for (const [second, batch] of [
        [2, items.slice(0, 1)],
        [1, items.slice(1, 101)],
        [0, items.slice(101)],
      ] as const) {
        clock.second = second;
        await session.addItems([...batch]);
      }
      const history = await session.getItems();
      expect(history).toEqual(items);
      if (method === "getItems") expect(await session.getItems(1)).toEqual(history.slice(-1));
      else {
        expect(await session.popItem()).toEqual(history.at(-1));
        expect(await session.getItems()).toEqual(history.slice(0, -1));
      }
    },
  );

  it.each<AgentInputItem>([
    {
      type: "function_call_result",
      name: "image",
      callId: "image-1",
      status: "completed",
      output: { type: "image", image: { data: new Uint8Array([137, 80, 78, 71]), mediaType: "image/png" } },
    },
    {
      type: "function_call_result",
      name: "image",
      callId: "image-2",
      status: "completed",
      output: { type: "image", image: { data: new Uint8Array() } },
    },
    {
      type: "function_call_result",
      name: "file",
      callId: "file-1",
      status: "completed",
      output: {
        type: "file",
        file: { data: new Uint8Array(3000).fill(65), mediaType: "text/plain", filename: "example.txt" },
      },
    },
  ])("preserves native binary tool output $callId without changing ordinary JSON", async (entry) => {
    const { agent } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    const ordinary: AgentInputItem = {
      role: "user",
      content: "ordinary",
      providerData: { type: "file", file: { data: [65, 66] }, numericObject: { "0": 65, "1": 66 } },
    };
    await session.addItems([entry, ordinary]);
    expect(await session.getItems()).toEqual([entry, ordinary]);
    expect(await session.popItem()).toEqual(ordinary);
    expect(await session.popItem()).toEqual(entry);
  });

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
    const appends = calls.filter((c) => c.op === "memory.append_batch").map((c) => c.body);
    expect(appends.map((b) => (b.entries as unknown[]).length)).toEqual([3, 1]);
    const entries = appends.flatMap((b) => b.entries as Array<{ memory_key: string; value: string }>);
    expect(new Set(entries.map((entry) => entry.memory_key)).size).toBe(4);
    expect(entries.map((entry) => JSON.parse(entry.value))).toEqual([
      msg("user", "hi"),
      msg("assistant", "hello"),
      msg("user", "hi"),
      msg("assistant", "again"),
    ]);
    expect(new Set(appends.map((b) => b.user_id))).toEqual(new Set(["alice"]));
    expect(String(appends[0].session_id)).toMatch(/^oai-ts-[0-9a-f]{20}$/);
  });

  it("separates native TypeScript history from Python history", async () => {
    const { agent, calls } = rowsHarness();
    await new LoomSession(agent, "s1", { userId: "alice" }).addItems([msg("user", "x")]);
    // The digest matches Python, but its oai-py- session stores a different item format.
    expect(calls.find((call) => call.op === "memory.append_batch")?.body.session_id).toBe(
      "oai-ts-65872b142b2cfb23bddc",
    );
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
        memory_key: String(
          (
            calls.filter((call) => call.op === "memory.append_batch")[0].body.entries as Array<{ memory_key: string }>
          )[0].memory_key,
        ),
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

  it("returns a history spanning seconds in key order with a matching latest read", async () => {
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

  it.each(["getItems", "popItem"] as const)("fails %s when an item disappears during hydration", async (method) => {
    const { agent, calls, disappear } = rowsHarness();
    const session = new LoomSession(agent, "s1", { userId: "alice" });
    await session.addItems([msg("assistant", "x".repeat(5000))]);
    disappear.add(
      String(
        (calls.filter((call) => call.op === "memory.append_batch")[0].body.entries as Array<{ memory_key: string }>)[0]
          .memory_key,
      ),
    );
    await expect(session[method]()).rejects.toThrow("unavailable");
  });

  it.each(["\u001c", "\u001d", "\u001e", "\u001f", "\u0085"].flatMap((c) => [c, `${c}alice`, `alice${c}`]))(
    "refuses server-stripped identity whitespace %j before requests",
    (userId) => {
      const { agent, calls } = rowsHarness();
      expect(() => new LoomSession(agent, "s1", { userId })).toThrow("userId");
      expect(calls).toHaveLength(0);
    },
  );

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
    expect(calls.find((call) => call.op === "memory.append_batch")?.body.session_id).toBe(
      "oai-ts-c10f2e9c4ca65c3f8b98",
    );
  });

  it("trims long slash prefixes without scanning each suffix", async () => {
    const { agent } = rowsHarness();
    expect(() => new LoomSession(agent, "s", { keyPrefix: "/".repeat(100_000) })).toThrow("keyPrefix");
    const session = new LoomSession(agent, "s", { keyPrefix: `${"/".repeat(100_000)}notes${"/".repeat(100_000)}` });
    await session.addItems([{ role: "user", content: "kept" }]);
    expect(await session.getItems()).toEqual([{ role: "user", content: "kept" }]);
  });

  it("refuses a missing id, a user naming none and an empty prefix", () => {
    const { agent } = rowsHarness();
    expect(() => new LoomSession(agent, "")).toThrow("sessionId");
    for (const userId of ["-", " alice", "alice "]) {
      expect(() => new LoomSession(agent, "s1", { userId })).toThrow("userId");
    }
    expect(() => new LoomSession(agent, "s1", { keyPrefix: "/" })).toThrow("keyPrefix");
  });
});
