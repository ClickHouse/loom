/**
 * The AI SDK tools against a running Loom (same environment as live.test.ts):
 * a fact stored with loom_remember comes back through loom_recall.
 */
import { describe, expect, it } from "vitest";
import { Datapack, LoomClient } from "../../src/index.js";
import { loomTools } from "../../src/integrations/ai-sdk.js";

const base = process.env.LOOM_BASE_URL ?? "";
const key = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const ns = process.env.LOOM_NAMESPACE ?? `ts-sdk-ai-sdk-${Math.random().toString(16).slice(2, 8)}`;
const options = { toolCallId: "c1", messages: [] } as never;

describe.skipIf(!base || !key)("AI SDK tools on live loom", () => {
  const client = new LoomClient({ baseUrl: base, apiKey: key, consumer: "ts-sdk-integ" });
  const bot = new Datapack(org, ns, client).agent("ai-sdk-bot");

  it("remembers, then recalls", async () => {
    const { loom_remember, loom_recall } = loomTools(bot, { userId: "alice" });
    const stored = await loom_remember.execute?.(
      { memory_key: "seat_preference", value: "Alice prefers aisle seats on long flights" },
      options,
    );
    expect(JSON.parse(stored as string).stored).toBe(true);
    let keys: string[] = [];
    for (let i = 0; i < 20 && keys.length === 0; i++) {
      const out = JSON.parse((await loom_recall.execute?.({ query: "Alice aisle seat" }, options)) as string) as {
        memories: Array<{ memory_key: string }>;
      };
      keys = out.memories.map((m) => m.memory_key);
      if (keys.length === 0) await new Promise((r) => setTimeout(r, 250));
    }
    expect(keys[0]).toBe("seat_preference");
  });
});
