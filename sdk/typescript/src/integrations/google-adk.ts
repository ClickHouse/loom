/** Google ADK's memory service over Loom, for one app and its namespace. */
import type { BaseMemoryService, SearchMemoryRequest, SearchMemoryResponse, Session } from "@google/adk";
import type { Agent } from "../agent.js";
import { LoomError } from "../errors.js";
import { hasIdentityWhitespace } from "../whitespace.js";

export interface LoomMemoryServiceOptions {
  /** The ADK app this namespace serves. Give each app its own namespace. */
  appName: string;
  /** Maximum returned entries. User memories precede shared knowledge. Default 10. */
  topK?: number;
  /** Exclude abstaining and below-floor memory hits. Default true. */
  honourAbstain?: boolean;
  /** Queue fact extraction rather than wait for it. Default true. */
  defer?: boolean;
}

/**
 * ADK 2.5's BaseMemoryService. Sessions become extracted facts; search returns the
 * user's memories and the app namespace's documents. The service adds no store.
 */
export class LoomMemoryService implements BaseMemoryService {
  private readonly topK: number;

  constructor(
    private readonly agent: Agent,
    private readonly opts: LoomMemoryServiceOptions,
  ) {
    if (!opts.appName) throw new Error("appName must name the ADK app this service serves");
    this.topK = opts.topK ?? 10;
    if (!Number.isInteger(this.topK) || this.topK < 1 || this.topK > 500) {
      throw new Error("topK must be an integer between 1 and 500");
    }
  }

  private check(appName: string, userId: string): void {
    if (appName !== this.opts.appName) {
      throw new Error("Give each ADK app its own Loom namespace and memory service");
    }
    if (!userId || userId === "-" || userId === "*" || hasIdentityWhitespace(userId)) {
      throw new Error("userId must name a Loom user without surrounding whitespace");
    }
  }

  async addSessionToMemory(session: Session): Promise<void> {
    this.check(session.appName, session.userId);
    if (!session.id) throw new Error("The ADK session id must not be empty");
    const messages = session.events.flatMap((event) => {
      const content = (event.content?.parts ?? [])
        .filter((part) => !part.thought && part.text)
        .map((part) => part.text)
        .join(" ")
        .trim();
      return content
        ? [
            {
              role: event.author === "user" ? "user" : "assistant",
              content,
              timestamp: new Date(event.timestamp).toISOString(),
            },
          ]
        : [];
    });
    if (!messages.length) return;
    const sessionId = `adk-${await digest([session.userId, session.id])}`;
    for (let start = 0; start < messages.length; start += 400) {
      await this.agent.call(
        "memory.set_from_messages",
        { messages: messages.slice(start, start + 400), defer: this.opts.defer ?? true },
        { userId: session.userId, sessionId },
      );
    }
  }

  async searchMemory(request: SearchMemoryRequest): Promise<SearchMemoryResponse> {
    this.check(request.appName, request.userId);
    if (!request.query.trim()) return { memories: [] };
    const found = await this.agent.recall(request.query, {
      userId: request.userId,
      top_k: this.topK,
      namespace_scope: "this",
      bodies: "full",
      explain: false,
    });
    const honourAbstain = this.opts.honourAbstain ?? true;
    const hits =
      honourAbstain && found.abstain ? [] : found.results.filter((hit) => !(honourAbstain && hit.below_threshold));
    const memories = hits.slice(0, this.topK).map((hit) => ({
      content: { role: "user", parts: [{ text: hit.content_excerpt }] },
      ...((hit.source === "fact" || hit.source === "anchor") && hit.agent && hit.agent !== "*"
        ? { id: JSON.stringify([hit.memory_key, hit.session_id || "-", hit.agent]) }
        : {}),
      customMetadata: { similarity: hit.similarity, source: hit.source },
      ...(hit.valid_at ? { timestamp: hit.valid_at } : {}),
    }));
    if (memories.length < this.topK) {
      const knowledge = await this.agent.call("knowledge.search", {
        query: request.query,
        top_k: Math.min(100, this.topK - memories.length),
        namespace_scope: "this",
      });
      return {
        memories: [
          ...memories,
          ...knowledge.results.map((hit) => ({
            content: { role: "user", parts: [{ text: hit.content }] },
            customMetadata: { source: "knowledge", source_uri: hit.source_uri, chunk_idx: hit.chunk_idx },
          })),
        ].slice(0, this.topK),
      };
    }
    return { memories };
  }
}

async function digest(parts: string[]): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new LoomError("LoomMemoryService needs crypto.subtle to scope its writes");
  const bytes = await subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(parts)));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 20);
}
