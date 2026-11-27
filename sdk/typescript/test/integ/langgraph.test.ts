import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { Annotation, END, START, StateGraph } from "@langchain/langgraph";
import { describe, expect, it, vi } from "vitest";
import { Agent } from "../../src/agent.js";
import { LoomStore } from "../../src/integrations/langgraph.js";

// Run with a Loom you started: `LOOM_BASE_URL=... LOOM_API_KEY=... LOOM_PYTHON=../../.venv/bin/python npm run test:integ -- test/integ/langgraph.test.ts`.
const baseUrl = process.env.LOOM_BASE_URL ?? "";
const apiKey = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const namespace = process.env.LOOM_NAMESPACE ?? "langgraph-test";
// A Python with the checkout's `loom` SDK installed, e.g. `.venv/bin/python`; the cross-language test needs it.
const python = process.env.LOOM_PYTHON ?? "";

const options = { baseUrl, apiKey, consumer: "ts-sdk-integ" };
const agentName = "langgraph-test";

// Every test writes under its own root namespace segment and deletes only what it wrote.
async function cleanup(store: LoomStore, root: string) {
  for (const ns of await store.listNamespaces({ prefix: [root] })) {
    for (const item of await store.search(ns, { limit: 100 })) await store.delete(ns, item.key);
  }
}

// One call to the Python LoomStore over the same Loom identity; prints JSON.
const PYTHON_SIDE = `
import json
import os
import sys

from loom import Agent
from loom.integrations.langgraph import LoomStore

cfg = json.loads(sys.argv[1])
agent = Agent(cfg["org"], cfg["namespace"], cfg["agent"], base_url=cfg["base_url"], api_key=os.environ["LOOM_API_KEY"])
store = LoomStore(agent)
ns = tuple(cfg["ns"])
try:
    got = store.get(ns, "from-ts")
    found = store.search(ns, limit=10)
    store.put(ns, "from-py", {"writer": "python", "n": 2, "nested": {"ok": True}})
    print(json.dumps({
        "got": got.value if got else None,
        "search_keys": sorted(i.key for i in found),
    }))
finally:
    store.close()
`;

describe.skipIf(!baseUrl || !apiKey)("LangGraph.js LoomStore on live Loom", () => {
  it("serves a compiled graph's puts, gets, searches and deletes", async () => {
    const root = `lgjs-${randomUUID()}`;
    const ns = [root, "alice"];
    const agent = new Agent(org, namespace, agentName, options);
    const store = new LoomStore(agent, { tags: ["lgjs-integ"] });
    const State = Annotation.Root({ out: Annotation<Record<string, unknown>> });
    const graph = new StateGraph(State)
      .addNode("run", async (_state, config) => {
        const s = config.store;
        if (!s) throw new Error("the graph has no store");
        await s.put(ns, "seat", { seat: "aisle", rank: 1 });
        await s.put(ns, "meal", { meal: "vegetarian", rank: 2 });
        const seat = await s.get(ns, "seat");
        const unranked = await s.search(ns, { limit: 10 });
        const filtered = await s.search(ns, { filter: { rank: { $gt: 1 } }, limit: 10 });
        await s.delete(ns, "seat");
        const afterDelete = await s.get(ns, "seat");
        return {
          out: {
            seat: seat?.value,
            unranked: unranked.map((i) => i.key).sort(),
            filtered: filtered.map((i) => i.key),
            afterDelete,
          },
        };
      })
      .addEdge(START, "run")
      .addEdge("run", END)
      .compile({ store });
    try {
      const { out } = await graph.invoke({ out: {} });
      expect(out).toEqual({
        seat: { seat: "aisle", rank: 1 },
        unranked: ["meal", "seat"],
        filtered: ["meal"],
        afterDelete: null,
      });
      expect(await store.listNamespaces({ prefix: [root] })).toEqual([ns]);
      // Ranked search follows the write once the memory is indexed.
      await vi.waitFor(
        async () => {
          const hits = await store.search(ns, { query: "vegetarian meal", limit: 5 });
          expect(hits.map((h) => h.key)).toContain("meal");
          expect(hits[0].score).toEqual(expect.any(Number));
        },
        { timeout: 60_000, interval: 2_000 },
      );
    } finally {
      await cleanup(store, root);
    }
    expect(await store.get(ns, "meal")).toBeNull();
  });

  it.skipIf(!python)("reads a Python LoomStore write and is read by it", async () => {
    const root = `lgjs-${randomUUID()}`;
    const ns = [root, "interop"];
    const agent = new Agent(org, namespace, agentName, options);
    const store = new LoomStore(agent);
    try {
      await store.put(ns, "from-ts", { writer: "typescript", n: 1, list: [1, "two", null] });
      const stdout = execFileSync(
        python,
        [
          "-c",
          PYTHON_SIDE,
          JSON.stringify({
            org,
            namespace,
            agent: agentName,
            base_url: baseUrl,
            ns,
          }),
        ],
        { encoding: "utf8", timeout: 90_000 },
      );
      const seen = JSON.parse(stdout.trim().split("\n").at(-1) ?? "{}");
      expect(seen.search_keys).toEqual(["from-ts"]);
      expect(seen.got).toEqual({ writer: "typescript", n: 1, list: [1, "two", null] });
      const fromPython = await store.get(ns, "from-py");
      expect(fromPython?.value).toEqual({ writer: "python", n: 2, nested: { ok: true } });
      expect((await store.search(ns, { limit: 10 })).map((i) => i.key).sort()).toEqual(["from-py", "from-ts"]);
    } finally {
      await cleanup(store, root);
    }
  });
});
