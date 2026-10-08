/**
 * Live smoke against a running Loom. Set LOOM_BASE_URL and LOOM_API_KEY (a
 * writer token on LOOM_ORG); LOOM_NAMESPACE defaults to a fresh throwaway
 * namespace that is registered here and left for `namespaces-gc`.
 *
 *   LOOM_BASE_URL=http://127.0.0.1:7777 LOOM_API_KEY=… LOOM_ORG=dev npm run test:integ
 */
import { describe, expect, it } from "vitest";
import { collect, Datapack, LoomClient, NotFound, paginate, probe, ValidationError } from "../../src/index.js";

const base = process.env.LOOM_BASE_URL ?? "";
const key = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const ns = process.env.LOOM_NAMESPACE ?? `ts-sdk-smoke-${Math.random().toString(16).slice(2, 8)}`;

describe.skipIf(!base || !key)("live loom", () => {
  const client = new LoomClient({ baseUrl: base, apiKey: key, consumer: "ts-sdk-integ" });
  const bot = new Datapack(org, ns, client).agent("smoke-bot");

  it("probes the deployment and finds the ops the agent loop needs", async () => {
    const p = await probe(client, {
      ops: ["memory.set", "memory.search", "memory.ask", "context.pack"],
      fields: { "memory.search": ["exclude_keys"] },
    });
    expect(p.missingOps).toEqual([]);
    expect(p.buildCommit).toMatch(/.+/);
  });

  it("registers a namespace, remembers, recalls with the identity stamped, and asks", async () => {
    await client
      .callRaw("namespace.register", { org, namespace: ns, contact_email: "ts-sdk@example.test", kind: "bench" })
      .catch(() => undefined);
    const set = await bot.remember("smoke_seat_pref", "The user prefers an aisle seat on long flights.", {
      userId: "u-smoke",
      tags: ["smoke"],
    });
    expect(set).toBeTruthy();
    const hits = await bot.recall("which seat does the user like", { userId: "u-smoke", top_k: 5 } as never);
    expect(Array.isArray(hits.results)).toBe(true);
    const keys = (hits.results as Array<{ memory_key: string }>).map((h) => h.memory_key);
    expect(keys).toContain("smoke_seat_pref");
    const answer = await bot.ask("Which seat does the user prefer?", { userId: "u-smoke" });
    expect(typeof answer.answer === "string" || answer.answer === null).toBe(true);
  });

  it("reads a missing key as found=false, maps a bad body to ValidationError and an unknown op to NotFound", async () => {
    const miss = await bot.call("memory.get", { memory_key: `does-not-exist-${Date.now()}` } as never, {
      userId: "u-smoke",
    });
    expect((miss as { found?: boolean; value?: unknown }).found ?? (miss as { value?: unknown }).value).toBeFalsy();
    await expect(client.callRaw("memory.search", { org, namespace: ns })).rejects.toBeInstanceOf(ValidationError);
    await expect(client.callRaw("memory.does_not_exist", { org, namespace: ns })).rejects.toBeInstanceOf(NotFound);
  });

  it("paginates knowledge.list through the cursor helper", async () => {
    const rows = await collect(
      paginate((cursor) => client.knowledge.list({ org, namespace: ns, limit: 2, cursor } as never)),
      10,
    );
    expect(Array.isArray(rows)).toBe(true);
  });
});
