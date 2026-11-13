/**
 * The OpenAI Agents SDK tools against a running Loom (same environment as
 * live.test.ts): a fact stored with loom_remember comes back through loom_recall.
 */
import { describe, expect, it } from "vitest";
import { Datapack, LoomClient } from "../../src/index.js";
import { loomTools } from "../../src/integrations/openai-agents.js";

const base = process.env.LOOM_BASE_URL ?? "";
const key = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const ns = process.env.LOOM_NAMESPACE ?? `ts-sdk-openai-agents-${Math.random().toString(16).slice(2, 8)}`;

describe.skipIf(!base || !key)("OpenAI Agents SDK tools on live loom", () => {
  const client = new LoomClient({ baseUrl: base, apiKey: key, consumer: "ts-sdk-integ" });
  const bot = new Datapack(org, ns, client).agent("openai-agents-bot");

  it("remembers, then recalls", async () => {
    const tools = Object.fromEntries(loomTools(bot, { userId: "alice" }).map((t) => [t.name, t]));
    const stored = await tools.loom_remember.invoke(
      {} as never,
      JSON.stringify({ memory_key: "seat_preference", value: "Alice prefers aisle seats on long flights" }),
    );
    expect(JSON.parse(stored as string).stored).toBe(true);
    let keys: string[] = [];
    for (let i = 0; i < 20 && keys.length === 0; i++) {
      const out = JSON.parse(
        (await tools.loom_recall.invoke({} as never, JSON.stringify({ query: "Alice aisle seat" }))) as string,
      ) as { memories: Array<{ memory_key: string }> };
      keys = out.memories.map((m) => m.memory_key);
      if (keys.length === 0) await new Promise((r) => setTimeout(r, 250));
    }
    expect(keys[0]).toBe("seat_preference");
  });
});
