/**
 * The LangGraph.js store against a running Loom (same environment as
 * live.test.ts): items written through `LoomStore` read back, search and list
 * by namespace, and delete.
 */
import { describe, expect, it } from "vitest";
import { Datapack, LoomClient } from "../../src/index.js";
import { LoomStore } from "../../src/integrations/langgraph.js";

const base = process.env.LOOM_BASE_URL ?? "";
const key = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const ns = process.env.LOOM_NAMESPACE ?? `ts-sdk-langgraph-${Math.random().toString(16).slice(2, 8)}`;

describe.skipIf(!base || !key)("LangGraph.js LoomStore on live loom", () => {
  const client = new LoomClient({ baseUrl: base, apiKey: key, consumer: "ts-sdk-integ" });
  const bot = new Datapack(org, ns, client).agent("langgraph-bot");
  const store = new LoomStore(bot);
  const user = `u${Date.now()}`;

  it("puts and gets an item", async () => {
    await store.put(["users", user], "prefs", { seat: "aisle", n: 3 });
    const item = await store.get(["users", user], "prefs");
    expect(item).toMatchObject({ key: "prefs", namespace: ["users", user], value: { seat: "aisle", n: 3 } });
    expect(item?.createdAt).toBeInstanceOf(Date);
  });

  it("lists items under a namespace prefix and applies a filter", async () => {
    await store.put(["users", user], "diet", { kind: "diet", n: 9 });
    const all = await store.search(["users", user]);
    expect(all.map((i) => i.key).sort()).toEqual(["diet", "prefs"]);
    const big = await store.search(["users", user], { filter: { n: { $gt: 5 } } });
    expect(big.map((i) => i.key)).toEqual(["diet"]);
  });

  it("lists the namespace", async () => {
    const namespaces = await store.listNamespaces({ prefix: ["users"] });
    expect(namespaces).toContainEqual(["users", user]);
  });

  it("finds an item by meaning", async () => {
    await store.put(["users", user], "flight", { note: "Alice prefers aisle seats on long flights" });
    let hits = await store.search(["users", user], { query: "which seat does Alice like on long flights" });
    for (let i = 0; i < 20 && hits.length === 0; i++) {
      // the write is searchable once its embedding lands
      await new Promise((r) => setTimeout(r, 250));
      hits = await store.search(["users", user], { query: "which seat does Alice like on long flights" });
    }
    expect(hits.map((h) => h.key)).toContain("flight");
    expect(typeof hits[0].score).toBe("number");
  });

  it("deletes an item", async () => {
    await store.delete(["users", user], "prefs");
    expect(await store.get(["users", user], "prefs")).toBeNull();
  });
});
