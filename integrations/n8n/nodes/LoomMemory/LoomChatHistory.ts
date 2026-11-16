import { createHash, randomBytes } from "node:crypto";
import type { Agent } from "@clickhouse/loom";
import { BaseListChatMessageHistory } from "@langchain/core/chat_history";
import {
  type BaseMessage,
  type StoredMessage,
  mapChatMessagesToStoredMessages,
  mapStoredMessagesToChatMessages,
} from "@langchain/core/messages";

type Row = { memory_key: string; value?: unknown; value_truncated?: boolean };
const PREFIX = "n8n-history/";
const ITEM = /^\d{20}-[0-9a-f]{8}$/;
let lastStamp = 0n;

export class LoomChatHistory extends BaseListChatMessageHistory {
  lc_namespace = ["loom", "n8n", "history"];
  private readonly scope: { userId?: string; sessionId: string };

  constructor(
    private readonly agent: Agent,
    sessionId: string,
    userId?: string,
  ) {
    super();
    if (!sessionId.trim()) throw new Error("Session ID must not be empty");
    const digest = createHash("sha256")
      .update(JSON.stringify([userId ?? "-", sessionId]))
      .digest("hex");
    this.scope = { userId, sessionId: `n8n-${digest.slice(0, 20)}` };
  }

  private async rows(): Promise<Row[]> {
    const rows = new Map<string, Row>();
    let offset = 0;
    for (;;) {
      const page = await this.agent.call(
        "memory.list",
        {
          prefix: PREFIX,
          memory_type: "episodic",
          limit: 500,
          offset,
          fields: ["memory_key", "value", "value_truncated"],
        },
        this.scope,
      );
      const entries = page.entries as Row[];
      for (const row of entries) {
        if (ITEM.test(row.memory_key.slice(PREFIX.length))) rows.set(row.memory_key, row);
      }
      if (!page.truncated) break;
      offset += Math.max(1, entries.length);
    }
    return [...rows.values()].sort((a, b) =>
      a.memory_key < b.memory_key ? -1 : a.memory_key > b.memory_key ? 1 : 0,
    );
  }

  async getMessages(): Promise<BaseMessage[]> {
    const messages: StoredMessage[] = [];
    for (const row of await this.rows()) {
      let value = row.value;
      if (row.value_truncated || value === undefined || value === null) {
        const full = await this.agent.call(
          "memory.get",
          { memory_key: row.memory_key },
          this.scope,
        );
        value = full.value;
      }
      if (value === undefined || value === null) throw new Error("A conversation item is missing");
      messages.push((typeof value === "string" ? JSON.parse(value) : value) as StoredMessage);
    }
    return mapStoredMessagesToChatMessages(messages);
  }

  async addMessage(message: BaseMessage): Promise<void> {
    const stamp = BigInt(Date.now()) * 1_000_000n;
    lastStamp = stamp > lastStamp ? stamp : lastStamp + 1n;
    await this.agent.call(
      "memory.set",
      {
        memory_key: `${PREFIX}${lastStamp.toString().padStart(20, "0")}-${randomBytes(4).toString("hex")}`,
        value: JSON.stringify(mapChatMessagesToStoredMessages([message])[0]),
        memory_type: "episodic",
      },
      this.scope,
    );
  }

  async clear(): Promise<void> {
    for (const row of await this.rows()) {
      await this.agent.call("memory.delete", { memory_key: row.memory_key }, this.scope);
    }
  }
}
