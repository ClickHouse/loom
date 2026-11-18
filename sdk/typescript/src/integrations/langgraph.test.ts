import { describe, expect, it } from "vitest";
import { Agent } from "../agent.js";
import { LoomStore, matchesFilter, memoryKeyFor, parseMemoryKey, prefixTag, tagsFor } from "./langgraph.js";

type Row = Record<string, unknown>;
type Handler = (body: Record<string, unknown>) => unknown;

function harness(handlers: Record<string, Handler> = {}) {
  // biome-ignore lint/suspicious/noExplicitAny: a request body the tests read field by field
  const calls: Array<{ op: string; body: Record<string, any> }> = [];
  const fetch = async (url: string, init: RequestInit) => {
    const op = url.split("/v1/")[1];
    const body = JSON.parse(init.body as string);
    calls.push({ op, body });
    const reply = handlers[op]?.(body) ?? { version: 1 };
    return new Response(JSON.stringify(reply), { status: 200 });
  };
  return { agent: new Agent("o", "n", "bot", { baseUrl: "http://x", fetch, logLevel: "off" }), calls };
}

const row = (value: unknown, extra: Row = {}): Row => ({
  found: true,
  value,
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-02T10:00:00Z",
  ...extra,
});

describe("key and tag mapping (the Python store's, byte for byte)", () => {
  it("maps a namespace and key to the memory key and one tag per prefix", () => {
    expect(memoryKeyFor(["users", "alice"], "prefs")).toBe("lg/users/alice/prefs");
    expect(parseMemoryKey("lg/users/alice/prefs")).toEqual({ namespace: ["users", "alice"], key: "prefs" });
    expect(parseMemoryKey("lg/only")).toBeNull();
    expect(parseMemoryKey("other/users/alice/prefs")).toBeNull();
    expect(parseMemoryKey("lg//x")).toBeNull();
    expect(tagsFor(["users", "alice"])).toEqual(["lg:users", "lg:users/alice"]);
    expect(prefixTag(["users"])).toBe("lg:users");
    expect(prefixTag([])).toBeUndefined();
  });

  it("rejects an empty segment and one holding a slash", () => {
    expect(() => memoryKeyFor(["a/b"], "k")).toThrow(/namespace segment/);
    expect(() => memoryKeyFor(["a"], "")).toThrow(/key/);
    expect(() => memoryKeyFor([""], "k")).toThrow(/namespace segment/);
  });
});

describe("matchesFilter", () => {
  it("is equality for a plain value and applies LangGraph's operators", () => {
    const v = { kind: "pref", n: 5, tags: ["a", "b"] };
    expect(matchesFilter(v, { kind: "pref" })).toBe(true);
    expect(matchesFilter(v, { kind: "other" })).toBe(false);
    expect(matchesFilter(v, { n: { $gt: 3, $lte: 5 } })).toBe(true);
    expect(matchesFilter(v, { n: { $gte: 6 } })).toBe(false);
    expect(matchesFilter(v, { n: { $lt: 5 } })).toBe(false);
    expect(matchesFilter(v, { kind: { $ne: "x" } })).toBe(true);
    expect(matchesFilter(v, { tags: ["a", "b"] })).toBe(true);
    expect(matchesFilter(v, { missing: { $gt: 1 } })).toBe(false);
    expect(matchesFilter(v, { n: { $gt: "3" } })).toBe(false);
    expect(matchesFilter(v, null)).toBe(true);
  });

  it("refuses an unknown operator", () => {
    expect(() => matchesFilter({ n: 1 }, { n: { $regex: "x" } })).toThrow(/unsupported filter operator/);
  });
});

describe("LoomStore.put / delete", () => {
  it("writes the memory key, tags, type and TTL the Python store writes for the same put", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent, { tags: ["tenant:acme"] });
    await store.batch([
      { namespace: ["users", "alice"], key: "prefs", value: { seat: "aisle" }, ttl: 5 } as never,
      { namespace: ["users", "alice"], key: "prefs", value: { seat: "aisle" }, index: false },
    ]);
    expect(calls.map((c) => c.op)).toEqual(["memory.set", "memory.set"]);
    expect(calls[0].body).toMatchObject({
      memory_key: "lg/users/alice/prefs",
      value: { seat: "aisle" },
      tags: ["tenant:acme", "lg:users", "lg:users/alice"],
      memory_type: "semantic",
      ttl_seconds: 300,
      org: "o",
      namespace: "n",
      agent: "bot",
    });
    expect(calls[1].body.memory_type).toBe("episodic");
    expect(calls[1].body).not.toHaveProperty("ttl_seconds");
  });

  it("floors a sub-minute TTL at one second", async () => {
    const { agent, calls } = harness();
    await new LoomStore(agent).batch([{ namespace: ["u"], key: "k", value: {}, ttl: 0.001 } as never]);
    expect(calls[0].body.ttl_seconds).toBe(1);
  });

  it("put() and delete() go through batch; a null value is memory.delete", async () => {
    const { agent, calls } = harness();
    const store = new LoomStore(agent);
    await store.put(["users", "alice"], "prefs", { a: 1 });
    await store.delete(["users", "alice"], "prefs");
    expect(calls.map((c) => c.op)).toEqual(["memory.set", "memory.delete"]);
    expect(calls[1].body.memory_key).toBe("lg/users/alice/prefs");
  });
});

describe("LoomStore.get", () => {
  it("returns an Item with Date timestamps, or null when absent", async () => {
    const { agent } = harness({
      "memory.get": (b) => (b.memory_key === "lg/u/here" ? row({ x: 1 }) : { found: false }),
    });
    const store = new LoomStore(agent);
    const item = await store.get(["u"], "here");
    expect(item).toEqual({
      value: { x: 1 },
      key: "here",
      namespace: ["u"],
      createdAt: new Date("2026-11-01T10:00:00Z"),
      updatedAt: new Date("2026-11-02T10:00:00Z"),
    });
    expect(await store.get(["u"], "gone")).toBeNull();
  });

  it("wraps a scalar Loom value", async () => {
    const { agent } = harness({ "memory.get": () => row("plain text") });
    expect((await new LoomStore(agent).get(["u"], "k"))?.value).toEqual({ value: "plain text" });
  });
});

describe("LoomStore.search", () => {
  it("with a query ranks with memory.search, then one bulk_get, dropping keys outside the prefix", async () => {
    const hits = [
      ["lg/users/alice/prefs", 0.9],
      ["lg/users/bob/prefs", 0.8],
      ["lg/teams/x/rule", 0.7],
      ["lg/users/alice/diet", 0.6],
      ["prefs/not-ours", 0.5],
    ];
    const { agent, calls } = harness({
      "memory.search": () => ({ results: hits.map(([memory_key, similarity]) => ({ memory_key, similarity })) }),
      "memory.bulk_get": (b) => ({
        results: Object.fromEntries((b.memory_keys as string[]).map((k) => [k, row({ k })])),
      }),
    });
    const out = await new LoomStore(agent).search(["users", "alice"], { query: "food", limit: 5 });
    expect(out.map((i) => [i.namespace, i.key, i.score])).toEqual([
      [["users", "alice"], "prefs", 0.9],
      [["users", "alice"], "diet", 0.6],
    ]);
    const search = calls.find((c) => c.op === "memory.search");
    expect(search?.body).toMatchObject({ query: "food", tags: ["lg:users/alice"], include_tags: false });
    expect(search?.body.top_k).toBeGreaterThanOrEqual(5);
    expect(calls.filter((c) => c.op === "memory.bulk_get")).toHaveLength(1);
    expect(calls.some((c) => c.op === "memory.list")).toBe(false);
  });

  it("caps a ranked search's top_k at 50 and applies offset and limit to the items", async () => {
    const hits = Array.from({ length: 6 }, (_, i) => ({ memory_key: `lg/u/k${i}`, similarity: 1 - i / 10 }));
    const { agent, calls } = harness({
      "memory.search": () => ({ results: hits }),
      "memory.bulk_get": (b) => ({
        results: Object.fromEntries((b.memory_keys as string[]).map((k) => [k, row({})])),
      }),
    });
    const store = new LoomStore(agent);
    const page = await store.search(["u"], { query: "q", limit: 2, offset: 2 });
    expect(page.map((i) => i.key)).toEqual(["k2", "k3"]);
    await store.search(["u"], { query: "q", limit: 40, offset: 30 });
    expect(calls.filter((c) => c.op === "memory.search").map((c) => c.body.top_k)).toEqual([8, 50]);
  });

  it("applies filter operators to the stored value", async () => {
    const values: Record<string, Row> = {
      "lg/u/a": { n: 1 },
      "lg/u/b": { n: 5 },
    };
    const { agent } = harness({
      "memory.search": () => ({
        results: Object.keys(values).map((memory_key) => ({ memory_key, similarity: 0.5 })),
      }),
      "memory.bulk_get": (b) => ({
        results: Object.fromEntries((b.memory_keys as string[]).map((k) => [k, row(values[k])])),
      }),
    });
    const out = await new LoomStore(agent).search(["u"], { query: "q", filter: { n: { $gt: 3 } } });
    expect(out.map((i) => i.key)).toEqual(["b"]);
  });

  it("without a query lists by prefix and tag, with no score", async () => {
    const { agent, calls } = harness({
      "memory.list": () => ({
        entries: [{ memory_key: "lg/users/alice/prefs" }, { memory_key: "lg/users/alice/diet" }, { memory_key: "x" }],
        truncated: false,
      }),
      "memory.bulk_get": (b) => ({
        results: Object.fromEntries((b.memory_keys as string[]).map((k) => [k, row({ k })])),
      }),
    });
    const out = await new LoomStore(agent).search(["users", "alice"]);
    expect(out.map((i) => i.key)).toEqual(["prefs", "diet"]);
    expect(out[0]).not.toHaveProperty("score");
    const list = calls.find((c) => c.op === "memory.list");
    expect(list?.body).toMatchObject({
      prefix: "lg/users/alice/",
      tags: ["lg:users/alice"],
      limit: 1000,
      offset: 0,
      fields: ["memory_key"],
    });
  });

  it("an unranked search pages through memory.list until it has enough", async () => {
    const all = Array.from({ length: 5 }, (_, i) => `lg/t/t${i}/k`);
    const { agent, calls } = harness({
      "memory.list": (b) => {
        const off = b.offset as number;
        const entries = all.slice(off, off + 2).map((memory_key) => ({ memory_key }));
        return { entries, truncated: off + 2 < all.length };
      },
      "memory.bulk_get": (b) => ({
        results: Object.fromEntries((b.memory_keys as string[]).map((k) => [k, row({})])),
      }),
    });
    const out = await new LoomStore(agent).search(["t"], { limit: 3 });
    expect(out).toHaveLength(3);
    expect(calls.filter((c) => c.op === "memory.list").map((c) => c.body.offset)).toEqual([0, 2]);
  });
});

describe("LoomStore.listNamespaces", () => {
  const keys = ["lg/users/alice/prefs", "lg/users/alice/diet", "lg/users/bob/prefs", "lg/teams/x/rule", "other/y/z"];
  const handlers = {
    "memory.list": () => ({ entries: keys.map((memory_key) => ({ memory_key })), truncated: false }),
  };

  it("derives the distinct sorted namespaces from the keys under lg/", async () => {
    const { agent, calls } = harness(handlers);
    expect(await new LoomStore(agent).listNamespaces()).toEqual([
      ["teams", "x"],
      ["users", "alice"],
      ["users", "bob"],
    ]);
    expect(calls[0].body).toMatchObject({ prefix: "lg/" });
    expect(calls[0].body).not.toHaveProperty("tags");
  });

  it("applies prefix, suffix, wildcard, maxDepth, offset and limit", async () => {
    const store = new LoomStore(harness(handlers).agent);
    expect(await store.listNamespaces({ prefix: ["users"] })).toEqual([
      ["users", "alice"],
      ["users", "bob"],
    ]);
    expect(await store.listNamespaces({ suffix: ["bob"] })).toEqual([["users", "bob"]]);
    expect(await store.listNamespaces({ prefix: ["*", "x"] })).toEqual([["teams", "x"]]);
    expect(await store.listNamespaces({ maxDepth: 1 })).toEqual([["teams"], ["users"]]);
    expect(await store.listNamespaces({ offset: 1, limit: 1 })).toEqual([["users", "alice"]]);
  });
});

describe("LoomStore.batch", () => {
  it("returns one result per operation, in order, with undefined for a put", async () => {
    const { agent } = harness({ "memory.get": () => row({ x: 1 }) });
    const out = await new LoomStore(agent).batch([
      { namespace: ["u"], key: "a", value: { x: 1 } },
      { namespace: ["u"], key: "a" },
      { namespace: ["u"], key: "a", value: null },
    ]);
    expect(out[0]).toBeUndefined();
    expect(out[1]).toMatchObject({ key: "a", value: { x: 1 } });
    expect(out[2]).toBeUndefined();
  });

  it("is a BaseStore with TTL support", () => {
    const store = new LoomStore(harness().agent);
    expect(store.supportsTtl).toBe(true);
    expect(typeof store.search).toBe("function");
  });
});
