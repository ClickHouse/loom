import { describe, expect, it, vi } from "vitest";
import { Agent } from "../agent.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  SessionProtectionUnavailable,
} from "../session-protection.js";
import { LoomStore } from "./langgraph.js";

type Row = Record<string, unknown>;
type Reply = (op: string, body: Row) => Row;

function family() {
  const state = { value: "clean" as "clean" | "protected" };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.value = "protected";
    },
  });
  return { root, state };
}

// A stubbed server that keeps memories by key, so a write can be read back.
function harness(opts: { reply?: Reply; root?: SessionProtection } = {}) {
  const calls: Array<{ op: string; body: Row }> = [];
  const rows = new Map<string, Row>();
  const ranked: Array<[string, number]> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string) as Row;
    calls.push({ op, body });
    const key = body.memory_key as string;
    let out: Row;
    if (opts.reply) out = opts.reply(op, body);
    else if (op === "memory.search") {
      out = {
        results: ranked.map(([memory_key, similarity]) => ({
          memory_key,
          memory_type: "semantic",
          content_excerpt: "",
          distance: 1 - similarity,
          similarity,
        })),
      };
    } else if (op === "memory.list") {
      const keys = [...rows.keys()].filter((k) => k.startsWith((body.prefix as string) ?? "")).sort();
      const start = body.offset as number;
      const limit = body.limit as number;
      out = {
        entries: keys.slice(start, start + limit).map((memory_key) => ({
          memory_key,
          expires_at: rows.get(memory_key)?.expires_at,
        })),
        truncated: start + limit < keys.length,
      };
    } else if (op === "memory.bulk_get") {
      const keys = body.memory_keys as string[];
      out = {
        results: Object.fromEntries(keys.filter((k) => rows.has(k)).map((k) => [k, rows.get(k)])),
        missing: keys.filter((k) => !rows.has(k)),
      };
    } else if (op === "memory.set") {
      rows.set(key, { found: true, value: body.value, created_at: "2026-03-01T10:00:00Z" });
      out = { version: 1 };
    } else if (op === "memory.delete") out = { tombstoned: rows.delete(key), version: 2 };
    else out = rows.get(key) ?? { found: false };
    return new Response(JSON.stringify(out), { status: 200, headers: { "content-type": "application/json" } });
  };
  const agent = new Agent("acme", "support", "bot", {
    baseUrl: "http://inert.invalid",
    fetch,
    logLevel: "off",
    sessionProtection: opts.root,
  });
  return { agent, calls, rows, ranked };
}

function seed(rows: Map<string, Row>, memoryKey: string, value: Row = {}) {
  rows.set(memoryKey, { found: true, value, created_at: "2026-03-01T10:00:00Z" });
}

describe("LoomStore (LangGraph.js adapter)", () => {
  it("writes the key, prefix tags and JSON value the Python adapter writes", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent, { tags: ["project"] });
    await store.put(["users", "alice"], "prefs", { seat: "aisle", nested: { n: [1, 2] } });
    expect(calls).toHaveLength(1);
    expect(calls[0].op).toBe("memory.set");
    expect(calls[0].body).toMatchObject({
      org: "acme",
      namespace: "support",
      agent: "bot",
      memory_key: "lg/users/alice/prefs",
      value: { seat: "aisle", nested: { n: [1, 2] } },
      tags: ["project", "lg:users", "lg:users/alice"],
      memory_type: "semantic",
    });
    expect(calls[0].body).not.toHaveProperty("ttl_seconds");
  });

  it("round-trips put, get and delete, with Date timestamps", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.put(["users", "alice"], "prefs", { seat: "aisle" });
    const item = await store.get(["users", "alice"], "prefs");
    expect(item).toMatchObject({ namespace: ["users", "alice"], key: "prefs", value: { seat: "aisle" } });
    expect(item?.createdAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    expect(item?.updatedAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    await store.delete(["users", "alice"], "prefs");
    expect(await store.get(["users", "alice"], "prefs")).toBeNull();
    expect(calls.map((c) => c.op)).toEqual(["memory.set", "memory.get", "memory.delete", "memory.get"]);
    expect(calls[2].body.memory_key).toBe("lg/users/alice/prefs");
  });

  it("treats a timestamp without a zone as UTC and falls back to now for a missing one", async () => {
    const { agent } = harness({
      reply: () => ({ found: true, value: { a: 1 }, created_at: "2026-03-01 10:00:00" }),
    });
    const before = Date.now();
    const item = await new LoomStore(agent).get(["n"], "k");
    expect(item?.createdAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    expect(item?.updatedAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    const { agent: bare } = harness({ reply: () => ({ found: true, value: { a: 1 } }) });
    const now = (await new LoomStore(bare).get(["n"], "k"))?.createdAt.getTime() ?? 0;
    expect(now).toBeGreaterThanOrEqual(before);
  });

  it("wraps a value that is not an object, on write and on read", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.batch([{ namespace: ["n"], key: "s", value: "text" as never }]);
    await store.batch([{ namespace: ["n"], key: "a", value: [1, 2] as never }]);
    expect(calls[0].body.value).toEqual({ value: "text" });
    expect(calls[1].body.value).toEqual({ value: [1, 2] });
    const { agent: other } = harness({ reply: () => ({ found: true, value: "plain" }) });
    expect((await new LoomStore(other).get(["n"], "k"))?.value).toEqual({ value: "plain" });
  });

  it("returns one result per operation, in input order", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.put(["n"], "b", { v: "b" });
    calls.length = 0;
    const out = await store.batch([
      { namespace: ["n"], key: "missing" },
      { namespace: ["n"], key: "a", value: { v: "a" } },
      { namespace: ["n"], key: "a" },
      { namespace: ["n"], key: "b", value: null },
      { namespace: ["n"], key: "b" },
    ]);
    expect(out[0]).toBeNull();
    expect(out[1]).toBeNull();
    expect(out[2]).toMatchObject({ key: "a", value: { v: "a" } });
    expect(out[3]).toBeNull();
    expect(out[4]).toBeNull();
    expect(calls.map((c) => [c.op, c.body.memory_key])).toEqual([
      ["memory.get", "lg/n/missing"],
      ["memory.set", "lg/n/a"],
      ["memory.get", "lg/n/a"],
      ["memory.delete", "lg/n/b"],
      ["memory.get", "lg/n/b"],
    ]);
  });

  it("converts a direct batch ttl in minutes to seconds", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.batch([
      { namespace: ["n"], key: "five", value: { a: 1 }, ttl: 5 },
      { namespace: ["n"], key: "thirty", value: { a: 1 }, ttl: 30 },
      { namespace: ["n"], key: "tiny", value: { a: 1 }, ttl: 0.001 },
      { namespace: ["n"], key: "none", value: { a: 1 }, ttl: null },
    ]);
    expect(calls.map((c) => c.body.ttl_seconds)).toEqual([300, 1800, 1, undefined]);
  });

  it("rejects a ttl that is not a positive number before any request", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    for (const ttl of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, "5"]) {
      await expect(store.batch([{ namespace: ["n"], key: "k", value: { a: 1 }, ttl: ttl as never }])).rejects.toThrow(
        /ttl/,
      );
    }
    expect(calls).toEqual([]);
  });

  it("maps index: false to an episodic memory without a ttl, and any other index to semantic", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.batch([{ namespace: ["n"], key: "raw", value: { a: 1 }, index: false }]);
    await store.put(["n"], "fields", { a: 1 }, ["a"]);
    expect(calls[0].body.memory_type).toBe("episodic");
    expect(calls[0].body).not.toHaveProperty("ttl_seconds");
    expect(calls[1].body.memory_type).toBe("semantic");
  });

  it("rejects an empty namespace on a write but reads and deletes by exact key", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await expect(store.batch([{ namespace: [], key: "k", value: { a: 1 } }])).rejects.toThrow(/non-empty namespace/);
    expect(calls).toEqual([]);
    await store.batch([{ namespace: [], key: "k" }]);
    await store.batch([{ namespace: [], key: "k", value: null }]);
    expect(calls.map((c) => [c.op, c.body.memory_key])).toEqual([
      ["memory.get", "lg/k"],
      ["memory.delete", "lg/k"],
    ]);
  });

  it("rejects malformed segments and keys before any request", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    const bad: Array<[string[], string]> = [
      [["a/b"], "k"],
      [[""], "k"],
      [[1 as never], "k"],
      [["a"], ""],
      [["a"], "x/y"],
      [["a"], 7 as never],
    ];
    for (const [namespace, key] of bad) {
      await expect(store.batch([{ namespace, key, value: { a: 1 } }])).rejects.toThrow();
      await expect(store.batch([{ namespace, key }])).rejects.toThrow();
      await expect(store.batch([{ namespace, key, value: null }])).rejects.toThrow();
    }
    await expect(store.batch([{ namespace: "a" as never, key: "k" }])).rejects.toThrow(/array/);
    expect(calls).toEqual([]);
  });

  it("keeps '.' and ':' legal so items written by the Python adapter read back", async () => {
    const { agent, calls } = harness();
    await new LoomStore(agent).batch([{ namespace: ["a.b", "c:d"], key: "k.1" }]);
    expect(calls[0].body.memory_key).toBe("lg/a.b/c:d/k.1");
  });

  it("sends nothing from a batch when a later operation is invalid", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await expect(
      store.batch([
        { namespace: ["n"], key: "ok", value: { a: 1 } },
        { namespace: ["n/x"], key: "bad", value: { a: 1 } },
      ]),
    ).rejects.toThrow();
    expect(calls).toEqual([]);
  });
});

// bulk_get echoes `found` rows, so a seeded key reads back with its value.
describe("LoomStore search", () => {
  function rankedStore() {
    const h = harness();
    seed(h.rows, "lg/users/alice/prefs", { seat: "aisle", kind: "pref" });
    seed(h.rows, "lg/users/alice/diet", { food: "veg", kind: "pref" });
    seed(h.rows, "lg/users/bob/prefs", { seat: "window", kind: "pref" });
    seed(h.rows, "lg/teams/x/rule", { kind: "rule" });
    h.ranked.push(
      ["lg/users/alice/prefs", 0.9],
      ["lg/users/bob/prefs", 0.8],
      ["lg/teams/x/rule", 0.7], // outside the prefix
      ["lg/users/alice/diet", 0.6],
      ["unrelated/key", 0.5], // not this adapter's
      ["lg/prefs", 0.4], // no namespace segment: unparseable
      ["lg/users//x", 0.3], // an empty part: unparseable
      ["lg/users/gone/k", 0.2], // no row: dropped
    );
    return { ...h, store: new LoomStore(h.agent) };
  }

  it("ranks, drops out-of-prefix, unparseable and missing hits, and keeps the score", async () => {
    const { store, calls } = rankedStore();
    const hits = await store.search(["users"], { query: "seat", limit: 2 });
    expect(hits.map((h) => [h.namespace, h.key, h.score])).toEqual([
      [["users", "alice"], "prefs", 0.9],
      [["users", "bob"], "prefs", 0.8],
    ]);
    expect(hits[0].value).toEqual({ seat: "aisle", kind: "pref" });
    expect(hits[0].createdAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    expect(calls.map((c) => c.op)).toEqual(["memory.search", "memory.bulk_get"]);
    // one bulk read, for the in-prefix parseable candidates only
    expect(calls[1].body.memory_keys).toEqual([
      "lg/users/alice/prefs",
      "lg/users/bob/prefs",
      "lg/users/alice/diet",
      "lg/users/gone/k",
    ]);
  });

  it("keeps ranked hits and values under the same writer when two agents use one key", async () => {
    const key = "lg/users/alice/prefs";
    const writers = [
      { agent: "bot", value: { seat: "aisle" }, similarity: 0.6 },
      { agent: "other", value: { seat: "window" }, similarity: 0.9 },
    ];
    const { agent, calls } = harness({
      reply(op, body) {
        if (op === "memory.search") {
          const matches = writers.filter(
            (writer) => (body.agent === "*" || body.agent === writer.agent) && body.query === writer.value.seat,
          );
          return { results: matches.map(({ similarity }) => ({ memory_key: key, similarity })) };
        }
        expect(op).toBe("memory.bulk_get");
        expect(body.memory_keys).toEqual([key]);
        const writer = writers.find((writer) => writer.agent === body.agent);
        return { results: writer ? { [key]: { found: true, value: writer.value } } : {} };
      },
    });
    const store = new LoomStore(agent);
    expect(await store.search(["users", "alice"], { query: "window" })).toEqual([]);
    expect(calls.map((call) => call.op)).toEqual(["memory.search"]);
    expect(await store.search(["users", "alice"], { query: "aisle" })).toMatchObject([
      { key: "prefs", value: { seat: "aisle" }, score: 0.6 },
    ]);
  });

  it("keeps ranked hits in the bound namespace when the server defaults to all_mine", async () => {
    const key = "lg/users/alice/prefs";
    const rows = [
      { namespace: "support", agent: "bot", value: { seat: "aisle" }, similarity: 0.6 },
      { namespace: "travel", agent: "bot", value: { seat: "window" }, similarity: 0.9 },
    ];
    const { agent, calls } = harness({
      reply(op, body) {
        if (op === "memory.search") {
          const scope = body.namespace_scope ?? "all_mine";
          const matches = rows.filter(
            (row) =>
              row.agent === body.agent &&
              (scope === "all_mine" || row.namespace === body.namespace) &&
              row.value.seat === body.query,
          );
          return { results: matches.map(({ similarity }) => ({ memory_key: key, similarity })) };
        }
        expect(op).toBe("memory.bulk_get");
        expect(body.memory_keys).toEqual([key]);
        const row = rows.find((row) => row.namespace === body.namespace && row.agent === body.agent);
        return { results: row ? { [key]: { found: true, value: row.value } } : {} };
      },
    });
    const store = new LoomStore(agent);
    expect(await store.search(["users", "alice"], { query: "window" })).toEqual([]);
    expect(calls.map((call) => call.op)).toEqual(["memory.search"]);
    expect(await store.search(["users", "alice"], { query: "aisle" })).toMatchObject([
      { key: "prefs", value: { seat: "aisle" }, score: 0.6 },
    ]);
  });

  it("sends the prefix tag, top_k, private scope and the bound identity", async () => {
    const { store, calls } = rankedStore();
    await store.search(["users", "alice"], { query: "seat", limit: 3, offset: 1 });
    expect(calls[0].body).toMatchObject({
      org: "acme",
      namespace: "support",
      agent: "bot",
      query: "seat",
      tags: ["lg:users/alice"],
      top_k: 8,
      include_tags: false,
      scope: "private",
      namespace_scope: "this",
    });
    calls.length = 0;
    await store.search([], { query: "seat" });
    expect(calls[0].body).not.toHaveProperty("tags");
    expect(calls[0].body.top_k).toBe(20);
  });

  it("caps top_k at 50, or at the configured cap", async () => {
    const { agent, calls } = harness();
    await new LoomStore(agent).search(["u"], { query: "q", limit: 40, offset: 20 });
    expect(calls[0].body.top_k).toBe(50);
    await new LoomStore(agent, { searchTopKCap: 7 }).search(["u"], { query: "q", limit: 40 });
    expect(calls[1].body.top_k).toBe(7);
    expect(() => new LoomStore(agent, { searchTopKCap: 0 })).toThrow(RangeError);
    expect(() => new LoomStore(agent, { searchTopKCap: 501 })).toThrow(RangeError);
  });

  it("pages the ranked hits with offset and filters before slicing", async () => {
    const { store } = rankedStore();
    const page = await store.search(["users"], { query: "seat", limit: 5, offset: 2 });
    expect(page.map((h) => h.key)).toEqual(["diet"]);
    const only = await store.search(["users"], { query: "seat", filter: { food: "veg" }, limit: 5 });
    expect(only.map((h) => h.key)).toEqual(["diet"]);
    const second = await store.search(["users"], { query: "seat", filter: { kind: "pref" }, limit: 1, offset: 1 });
    expect(second.map((h) => h.key)).toEqual(["prefs"]);
    expect(second[0].namespace).toEqual(["users", "bob"]);
  });

  it("sends no request for a zero limit and a search with no hits sends no bulk read", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    expect(await store.search(["u"], { query: "q", limit: 0 })).toEqual([]);
    expect(calls).toEqual([]);
    expect(await store.search(["u"], { query: "q" })).toEqual([]);
    expect(calls.map((c) => c.op)).toEqual(["memory.search"]);
  });

  it("without a query lists by recency under the encoded prefix and tag", async () => {
    const { agent, calls, rows } = harness();
    seed(rows, "lg/users/alice/a", { n: 1 });
    seed(rows, "lg/users/alice/b", { n: 2 });
    seed(rows, "lg/users/alicia/c", { n: 3 }); // shares the text, not the segment
    seed(rows, "lg/users/bob/c", { n: 4 });
    const store = new LoomStore(agent);
    const items = await store.search(["users", "alice"]);
    expect(items.map((i) => [i.key, i.score])).toEqual([
      ["a", undefined],
      ["b", undefined],
    ]);
    expect(items[0]).not.toHaveProperty("score");
    expect(calls[0]).toMatchObject({
      op: "memory.list",
      body: {
        prefix: "lg/users/alice/",
        tags: ["lg:users/alice"],
        limit: 1000,
        offset: 0,
        fields: ["memory_key", "expires_at"],
      },
    });
    expect(calls.map((c) => c.op)).toEqual(["memory.list", "memory.bulk_get"]);
    expect((await store.search(["users", "alice"], { filter: { n: { $gte: 2 } } })).map((i) => i.key)).toEqual(["b"]);
    expect(await store.search(["nobody"])).toEqual([]);
    calls.length = 0;
    await store.search([]);
    expect(calls[0].body).toMatchObject({ prefix: "lg/" });
    expect(calls[0].body).not.toHaveProperty("tags");
  });

  it("skips an expired item and its namespace while the server still lists the row", async () => {
    const { agent, rows, calls } = harness();
    const expired = "lg/users/alice/a";
    rows.set(expired, { found: true, value: { expired: true }, expires_at: "2026-03-01 10:00:00" });
    seed(rows, "lg/users/bob/b");
    const store = new LoomStore(agent);
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-03-01T10:00:00Z"));
    try {
      expect((await store.search(["users"], { limit: 1 })).map((item) => item.key)).toEqual(["b"]);
      expect(calls.find((call) => call.op === "memory.bulk_get")?.body.memory_keys).toEqual(["lg/users/bob/b"]);
      expect(await store.listNamespaces()).toEqual([["users", "bob"]]);
      expect(rows.has(expired)).toBe(true);
    } finally {
      clock.mockRestore();
    }
  });

  it("skips a value that expires between memory.list and memory.bulk_get", async () => {
    const key = "lg/users/alice/a";
    const expires_at = "2026-03-01T11:00:01+01:00";
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-03-01T10:00:00Z"));
    try {
      const { agent } = harness({
        reply(op) {
          if (op === "memory.list") return { entries: [{ memory_key: key, expires_at }], truncated: false };
          expect(op).toBe("memory.bulk_get");
          clock.mockReturnValue(Date.parse(expires_at));
          return { results: { [key]: { found: true, value: { expired: true }, expires_at } } };
        },
      });
      expect(await new LoomStore(agent).search(["users"])).toEqual([]);
    } finally {
      clock.mockRestore();
    }
  });

  it("returns the first listed items in the server's order, then a later window", async () => {
    const { agent, rows } = harness();
    for (const k of ["a", "b", "c", "d"]) seed(rows, `lg/n/${k}`, { k });
    const store = new LoomStore(agent);
    expect((await store.search(["n"], { limit: 2 })).map((i) => i.key)).toEqual(["a", "b"]);
    expect((await store.search(["n"], { limit: 2, offset: 2 })).map((i) => i.key)).toEqual(["c", "d"]);
  });

  it("filters with each operator against JSON values", async () => {
    const { agent, rows } = harness();
    seed(rows, "lg/n/a", { kind: "pref", n: 3, tags: ["x", "y"], meta: { a: 1 }, none: null });
    const store = new LoomStore(agent);
    const hit = async (filter: Record<string, unknown>) => (await store.search(["n"], { filter })).length === 1;
    expect(await hit({})).toBe(true);
    expect(await hit({ kind: "pref" })).toBe(true);
    expect(await hit({ kind: "fact" })).toBe(false);
    expect(await hit({ tags: ["x", "y"] })).toBe(true);
    expect(await hit({ meta: { a: 1 } })).toBe(true);
    expect(await hit({ meta: { a: 2 } })).toBe(false);
    expect(await hit({ n: { $eq: 3 } })).toBe(true);
    expect(await hit({ n: { $ne: 4 } })).toBe(true);
    expect(await hit({ n: { $ne: 3 } })).toBe(false);
    expect(await hit({ n: { $gt: 2, $lte: 3 } })).toBe(true);
    expect(await hit({ n: { $gt: 3 } })).toBe(false);
    expect(await hit({ n: { $gte: 3 } })).toBe(true);
    expect(await hit({ n: { $gte: 4 } })).toBe(false);
    expect(await hit({ n: { $lt: 4 } })).toBe(true);
    expect(await hit({ n: { $lt: 3 } })).toBe(false);
    expect(await hit({ n: { $lte: 3 } })).toBe(true);
    expect(await hit({ n: { $lte: 2 } })).toBe(false);
    expect(await hit({ kind: { $gt: "a" } })).toBe(true);
    // missing or null: comparisons are false, equality sees null
    expect(await hit({ missing: { $gt: 0 } })).toBe(false);
    expect(await hit({ missing: { $lte: 0 } })).toBe(false);
    expect(await hit({ none: { $gte: 0 } })).toBe(false);
    expect(await hit({ missing: { $eq: null } })).toBe(true);
    expect(await hit({ missing: { $ne: 1 } })).toBe(true);
    // mixed types neither throw nor match
    expect(await hit({ kind: { $gt: 1 } })).toBe(false);
    expect(await hit({ n: { $lt: "9" } })).toBe(false);
    expect(await hit({ n: { $eq: "3" } })).toBe(false);
  });

  it("rejects an unknown filter operator before any request", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await expect(store.search(["n"], { filter: { n: { $in: [1] } } })).rejects.toThrow(/\$in/);
    await expect(store.search(["n"], { query: "q", filter: { n: { $gt: 1, $regex: "x" } } })).rejects.toThrow(
      /\$regex/,
    );
    await expect(store.search(["n"], { filter: "x" as never })).rejects.toThrow(/filter/);
    expect(calls).toEqual([]);
  });

  it("rejects an invalid prefix, limit or offset before any request", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await expect(store.batch([{ namespacePrefix: ["a/b"], limit: 1, offset: 0 }])).rejects.toThrow();
    await expect(store.batch([{ namespacePrefix: "a" as never, limit: 1, offset: 0 }])).rejects.toThrow(/array/);
    for (const limit of [-1, 1.5, Number.NaN, "2" as never]) {
      await expect(store.batch([{ namespacePrefix: ["n"], limit, offset: 0 }])).rejects.toThrow(/limit/);
    }
    await expect(store.batch([{ namespacePrefix: ["n"], limit: 1, offset: -1 }])).rejects.toThrow(/offset/);
    expect(calls).toEqual([]);
  });

  it("wraps scalar values and reads timestamps like get", async () => {
    const { agent, rows } = harness();
    rows.set("lg/n/s", {
      found: true,
      value: "text",
      created_at: "2026-03-01 10:00:00",
      updated_at: "2026-03-02T11:00:00Z",
    });
    const [item] = await new LoomStore(agent).search(["n"]);
    expect(item.value).toEqual({ value: "text" });
    expect(item.createdAt).toEqual(new Date("2026-03-01T10:00:00Z"));
    expect(item.updatedAt).toEqual(new Date("2026-03-02T11:00:00Z"));
  });

  it("pages an unranked search past one memory.list page and stops once it has enough", async () => {
    const { agent, calls, rows } = harness();
    for (let i = 0; i < 2500; i++) seed(rows, `lg/team/t${String(i).padStart(4, "0")}/k`, { i });
    const page = await new LoomStore(agent).search(["team"], { limit: 2, offset: 1500 });
    expect(page.map((p) => p.value.i)).toEqual([1500, 1501]);
    expect(calls.filter((c) => c.op === "memory.list").map((c) => c.body.offset)).toEqual([0, 1000]);
    expect(calls.filter((c) => c.op === "memory.bulk_get").map((c) => (c.body.memory_keys as string[]).length)).toEqual(
      [1000, 1000],
    );
  });

  it("advances the next page by the count the server returned", async () => {
    const offsets: unknown[] = [];
    const { agent } = harness({
      reply: (op, body) => {
        if (op === "memory.bulk_get") return { results: {} };
        offsets.push(body.offset);
        return { entries: [{ memory_key: "lg/n/a" }, { memory_key: "lg/n/b" }], truncated: offsets.length < 3 };
      },
    });
    await new LoomStore(agent).search(["n"], { limit: 50 });
    expect(offsets).toEqual([0, 2, 4]);
  });

  it("stops paging when a truncated page is empty", async () => {
    const { agent, calls } = harness({ reply: () => ({ entries: [], truncated: true }) });
    expect(await new LoomStore(agent).search(["n"])).toEqual([]);
    expect(calls).toHaveLength(1);
  });
});

describe("LoomStore listNamespaces", () => {
  function stored() {
    const h = harness();
    seed(h.rows, "lg/users/alice/prefs/k");
    seed(h.rows, "lg/users/bob/k");
    seed(h.rows, "lg/teams/x/k");
    seed(h.rows, "lg/teams/x/k2");
    seed(h.rows, "lg/lonely"); // no namespace
    seed(h.rows, "lg/a//k"); // empty part
    seed(h.rows, "other/thing");
    return { ...h, store: new LoomStore(h.agent) };
  }

  it("derives sorted, de-duplicated namespaces from the keys under lg/", async () => {
    const { store, calls } = stored();
    expect(await store.listNamespaces()).toEqual([
      ["teams", "x"],
      ["users", "alice", "prefs"],
      ["users", "bob"],
    ]);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      op: "memory.list",
      body: { prefix: "lg/", limit: 1000, offset: 0, fields: ["memory_key", "expires_at"] },
    });
    expect(calls[0].body).not.toHaveProperty("tags");
  });

  it("applies prefix, wildcard and suffix conditions", async () => {
    const { store } = stored();
    expect(await store.listNamespaces({ prefix: ["users"] })).toEqual([
      ["users", "alice", "prefs"],
      ["users", "bob"],
    ]);
    expect(await store.listNamespaces({ prefix: ["users", "*"] })).toEqual([
      ["users", "alice", "prefs"],
      ["users", "bob"],
    ]);
    expect(await store.listNamespaces({ prefix: ["*", "x"] })).toEqual([["teams", "x"]]);
    expect(await store.listNamespaces({ suffix: ["prefs"] })).toEqual([["users", "alice", "prefs"]]);
    expect(await store.listNamespaces({ suffix: ["*", "bob"] })).toEqual([["users", "bob"]]);
    expect(await store.listNamespaces({ prefix: ["users"], suffix: ["bob"] })).toEqual([["users", "bob"]]);
    expect(await store.listNamespaces({ prefix: ["a", "b", "c", "d", "e"] })).toEqual([]);
  });

  it("truncates to maxDepth before de-duplicating, then pages with limit and offset", async () => {
    const { store } = stored();
    expect(await store.listNamespaces({ maxDepth: 1 })).toEqual([["teams"], ["users"]]);
    expect(await store.listNamespaces({ maxDepth: 2 })).toEqual([
      ["teams", "x"],
      ["users", "alice"],
      ["users", "bob"],
    ]);
    expect(await store.listNamespaces({ limit: 1, offset: 1 })).toEqual([["users", "alice", "prefs"]]);
    expect(await store.listNamespaces({ limit: 0 })).toEqual([]);
  });

  it("lists prefixes the upstream validation would refuse", async () => {
    const { agent, rows } = harness();
    seed(rows, "lg/a.b/c:d/k");
    expect(await new LoomStore(agent).listNamespaces({ prefix: ["a.b"] })).toEqual([["a.b", "c:d"]]);
  });

  it("lists unfiltered namespaces in a batch without matchConditions", async () => {
    const { store } = stored();
    expect(
      await store.batch([
        { limit: 1, offset: 0 },
        { maxDepth: 1, limit: 1, offset: 1 },
      ]),
    ).toEqual([[["teams", "x"]], [["users"]]]);
  });

  it("pages past one memory.list page", async () => {
    const { agent, calls, rows } = harness();
    for (let i = 0; i < 2500; i++) seed(rows, `lg/team/t${String(i).padStart(4, "0")}/k`);
    const namespaces = await new LoomStore(agent).listNamespaces({ prefix: ["team"], limit: 5000 });
    expect(namespaces).toHaveLength(2500);
    expect(calls.map((c) => c.body.offset)).toEqual([0, 1000, 2000]);
  });

  it("rejects an unknown condition, a bad depth, limit or offset before any request", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await expect(
      store.batch([{ matchConditions: [{ matchType: "infix" as never, path: ["a"] }], limit: 1, offset: 0 }]),
    ).rejects.toThrow(/match condition/);
    await expect(store.listNamespaces({ maxDepth: -1 })).rejects.toThrow(/maxDepth/);
    await expect(store.listNamespaces({ limit: 1.5 })).rejects.toThrow(/limit/);
    await expect(store.listNamespaces({ offset: -1 })).rejects.toThrow(/offset/);
    expect(calls).toEqual([]);
  });
});

describe("LoomStore mixed batches", () => {
  it("returns results in order, with search and listNamespaces alongside writes and reads", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    const out = await store.batch([
      { namespace: ["u"], key: "k", value: { a: 1 } },
      { namespace: ["u"], key: "k" },
      { namespacePrefix: ["u"], limit: 10, offset: 0 },
      { matchConditions: undefined, limit: 100, offset: 0 },
      { matchConditions: [{ matchType: "prefix", path: ["u"] }], limit: 100, offset: 0 },
      { namespace: ["u"], key: "k", value: null },
      { namespace: ["u"], key: "k" },
    ]);
    expect(out[0]).toBeNull();
    expect(out[1]).toMatchObject({ key: "k", value: { a: 1 } });
    expect((out[2] as Array<{ key: string }>).map((i) => i.key)).toEqual(["k"]);
    expect(out[3]).toEqual([["u"]]);
    expect(out[4]).toEqual([["u"]]);
    expect(out[5]).toBeNull();
    expect(out[6]).toBeNull();
    expect(calls.map((c) => c.op)).toEqual([
      "memory.set",
      "memory.get",
      "memory.list",
      "memory.bulk_get",
      "memory.list",
      "memory.list",
      "memory.delete",
      "memory.get",
    ]);
  });
});

describe("LoomStore session protection", () => {
  const found = { found: true, value: { note: "private" } };

  it("an explicit store observes: a marked reply protects the family and is still returned", async () => {
    const { root, state } = family();
    const { agent } = harness({ root, reply: () => ({ ...found, mirrored: true }) });
    const item = await new LoomStore(agent).get(["u"], "k");
    expect(item?.value).toEqual({ note: "private" });
    expect(state.value).toBe("protected");
  });

  it("an explicit store stays usable once its family is protected", async () => {
    const { root } = family();
    await root.mark();
    const { agent, calls } = harness({ root });
    const store = new LoomStore(agent);
    await store.put(["u"], "k", { note: "explicit" });
    expect((await store.get(["u"], "k"))?.value).toEqual({ note: "explicit" });
    expect(calls.map((c) => c.op)).toEqual(["memory.set", "memory.get"]);
  });

  it("an automatic store sends nothing once its family is protected", async () => {
    const { root } = family();
    await root.mark();
    const { agent, calls } = harness({ root });
    const store = new LoomStore(agent, { capture: new SessionCapture([root]) });
    await expect(store.get(["u"], "k")).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    await expect(store.put(["u"], "k", { note: "private" })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    await expect(store.delete(["u"], "k")).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(calls).toEqual([]);
  });

  it("an automatic store stops the rest of a batch when a reply protects the family", async () => {
    const { root, state } = family();
    const { agent, calls } = harness({
      root,
      reply: (op) => (op === "memory.get" ? { ...found, mirrored: true } : { version: 1 }),
    });
    const store = new LoomStore(agent, { capture: new SessionCapture([root]) });
    await expect(
      store.batch([
        { namespace: ["u"], key: "k" },
        { namespace: ["u"], key: "next", value: { a: 1 } },
      ]),
    ).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(state.value).toBe("protected");
    expect(calls.map((c) => c.op)).toEqual(["memory.get"]);
  });

  it("an automatic search stops before its bulk read when the listing protects the family", async () => {
    const { root, state } = family();
    const { agent, calls } = harness({
      root,
      reply: () => ({ entries: [{ memory_key: "lg/n/a" }], truncated: false, mirrored: true }),
    });
    const store = new LoomStore(agent, { capture: new SessionCapture([root]) });
    await expect(store.search(["n"])).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(state.value).toBe("protected");
    expect(calls.map((c) => c.op)).toEqual(["memory.list"]);
  });

  it("an automatic store does not return a write that protected its family", async () => {
    const { root, state } = family();
    const { agent } = harness({ root, reply: () => ({ version: 1, mirrored: true }) });
    const store = new LoomStore(agent, { capture: new SessionCapture([root]) });
    await expect(store.put(["u"], "k", { a: 1 })).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(state.value).toBe("protected");
  });

  it("a malformed reply fails closed and protects the family", async () => {
    for (const capture of [false, true]) {
      const { root, state } = family();
      const { agent } = harness({ root, reply: () => ({ found: false, mirrored: "unknown" }) });
      const store = new LoomStore(agent, capture ? { capture: new SessionCapture([root]) } : {});
      await expect(store.get(["u"], "k")).rejects.toBeInstanceOf(SessionProtectionUnavailable);
      expect(state.value).toBe("protected");
    }
  });
});
