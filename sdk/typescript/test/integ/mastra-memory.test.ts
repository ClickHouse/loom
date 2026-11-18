import { randomUUID } from "node:crypto";
import { MastraCompositeStore } from "@mastra/core/storage";
import { Memory } from "@mastra/memory";
import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent.js";
import { LoomMemoryStorage } from "../../src/integrations/mastra.js";

process.env.MASTRA_TELEMETRY_DISABLED = "1";

const baseUrl = process.env.LOOM_BASE_URL ?? "";
const apiKey = process.env.LOOM_API_KEY ?? "";
const org = process.env.LOOM_ORG ?? "dev";
const namespace = process.env.LOOM_NAMESPACE ?? "mastra-memory-test";

describe.skipIf(!baseUrl || !apiKey)("Mastra memory on live Loom", () => {
  it("persists native history and working memory under the bound user and deletes only that store", async () => {
    const resourceId = `mastra-${randomUUID()}`;
    const bot = new Agent(org, namespace, "mastra-memory-test", { baseUrl, apiKey, consumer: "ts-sdk-integ" });
    const domain = () => new LoomMemoryStorage(bot, { resourceId, storeId: "live" });
    const foreign = new LoomMemoryStorage(
      new Agent(org, namespace, "mastra-other-agent", { baseUrl, apiKey, consumer: "ts-sdk-integ" }),
      { resourceId, storeId: "live" },
    );
    const native = () =>
      new Memory({
        storage: new MastraCompositeStore({ id: "live", domains: { memory: domain() } }),
        options: {
          semanticRecall: false,
          observationalMemory: false,
          workingMemory: { enabled: true, scope: "resource" },
        },
      });
    const createdAt = new Date("2025-01-01T00:00:00Z");
    try {
      await native().saveThread({ thread: { id: "t1", resourceId, title: "First", createdAt, updatedAt: createdAt } });
      await native().saveMessages({
        messages: [
          {
            id: "m1",
            threadId: "t1",
            resourceId,
            role: "user",
            createdAt,
            content: { format: 2, parts: [{ type: "text", text: "Stored history" }] },
          },
        ],
      });
      await native().updateThread({ id: "t1", metadata: { category: "work" } });
      expect((await domain().getThreadById({ threadId: "t1" }))?.title).toBe("First");
      await Promise.all([
        domain().updateResource({ resourceId, workingMemory: "Initial notes" }),
        domain().updateResource({ resourceId, metadata: { category: "work" } }),
      ]);
      expect(await domain().getResourceById({ resourceId })).toMatchObject({
        workingMemory: "Initial notes",
        metadata: { category: "work" },
      });
      await native().updateWorkingMemory({ threadId: "t1", resourceId, workingMemory: "Working notes" });
      expect(await native().getWorkingMemory({ threadId: "t1", resourceId })).toBe("Working notes");
      await expect
        .poll(
          async () =>
            (await native().recall({ threadId: "t1", resourceId, perPage: false })).messages.map(
              (message) => message.id,
            ),
          { timeout: 10_000 },
        )
        .toEqual(["m1"]);
      await domain().updateMessages({ messages: [{ id: "m1", role: "assistant" }] });
      expect((await domain().listMessagesById({ messageIds: ["m1"] })).messages[0]).toMatchObject({
        role: "assistant",
        createdAt,
      });
      const rows = await bot.call(
        "memory.list",
        { prefix: "mastra/", fields: ["user_id", "memory_key"], include_duplicates: true, include_superseded: true },
        { userId: resourceId },
      );
      expect(rows.entries.length).toBe(3);
      expect(rows.entries.every((row) => row.user_id === resourceId)).toBe(true);
      await native().deleteThread("t1");
      expect((await domain().listMessagesById({ messageIds: ["m1"] })).messages).toEqual([]);
      expect(await domain().getThreadById({ threadId: "t1" })).toBeNull();
      await foreign.saveThread({
        thread: { id: "t1", resourceId, title: "Other writer", createdAt, updatedAt: createdAt },
      });
      await domain().deleteThread({ threadId: "t1" });
      expect((await foreign.getThreadById({ threadId: "t1" }))?.title).toBe("Other writer");
      await domain().dangerouslyClearAll();
      await Promise.all([
        domain().updateResource({ resourceId, metadata: { first: true } }),
        domain().updateResource({ resourceId, metadata: { second: true } }),
      ]);
      const recreated = await domain().getResourceById({ resourceId });
      expect(recreated?.metadata).toEqual({ first: true, second: true });
      expect(recreated?.workingMemory).toBeUndefined();
    } finally {
      try {
        await domain().dangerouslyClearAll();
      } finally {
        await foreign.dangerouslyClearAll();
      }
    }
  });
});
