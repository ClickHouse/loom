/**
 * Loom as LangChain.js retrievers (the TypeScript twins of
 * `loom.integrations.langchain`).
 *
 * `LoomKnowledgeRetriever` searches the documents in a Loom namespace
 * (`knowledge.search`) and `LoomMemoryRetriever` its memories
 * (`memory.search`). Both extend `BaseRetriever` from `@langchain/core`, an
 * optional peer dependency, so they run anywhere a retriever does: `invoke`,
 * a chain, or a retriever tool.
 *
 *     const agent = new Datapack("acme", "support", { baseUrl, apiKey }).agent("assistant");
 *     const docs = await new LoomKnowledgeRetriever(agent, { k: 3 }).invoke("refund policy");
 *
 * Identity and isolation come from the Loom `Agent`: the retriever reads only
 * what its token may read.
 */

import type { CallbackManagerForRetrieverRun } from "@langchain/core/callbacks/manager";
import { Document } from "@langchain/core/documents";
import { BaseRetriever, type BaseRetrieverInput } from "@langchain/core/retrievers";
import type { Agent, Body, Scope } from "../agent.js";

type KnowledgeSearchBody = Body<"knowledge.search">;
type MemorySearchBody = Body<"memory.search">;

export interface LoomRetrieverOptions extends BaseRetrieverInput {
  /** Documents per query. Default 4. */
  k?: number;
  /** Only rows carrying at least one of these Loom tags. (`tags` is LangChain's callback tags.) */
  searchTags?: string[];
}

export interface LoomKnowledgeRetrieverOptions extends LoomRetrieverOptions {
  /** Only documents whose `source_uri` starts with one of these. */
  uriPrefix?: string[];
  /** `this` searches the agent's namespace; `all_mine` adds the caller's other namespaces, up to the server's fan-out cap. Default `this`. */
  namespaceScope?: NonNullable<KnowledgeSearchBody["namespace_scope"]>;
  /** Further `knowledge.search` fields (`search_mode`, `rerank`, ...). */
  searchKwargs?: Omit<KnowledgeSearchBody, "query">;
}

export interface LoomMemoryRetrieverOptions extends LoomRetrieverOptions {
  /** Only memories of this type. */
  memoryType?: NonNullable<MemorySearchBody["memory_type"]>;
  /** Only this user's, session's, run's or agent's memories; without an `agentId`, every agent's. */
  scope?: Scope;
  /** Return no documents when Loom abstains (no memory above its calibrated floor), and leave out memories below it. Default true. */
  honourAbstain?: boolean;
}

/** Chunks from `knowledge.search`, one `Document` per chunk; the id is `<namespace>/<source_uri>#<chunk_idx>`. */
export class LoomKnowledgeRetriever extends BaseRetriever {
  lc_namespace = ["loom", "retrievers", "knowledge"];
  private readonly k: number;
  private readonly body: Omit<KnowledgeSearchBody, "query">;

  constructor(
    private readonly agent: Agent,
    opts: LoomKnowledgeRetrieverOptions = {},
  ) {
    super(opts);
    this.k = opts.k ?? 4;
    this.body = {
      ...opts.searchKwargs,
      top_k: this.k,
      ...(opts.searchTags?.length ? { tags: [...opts.searchTags] } : {}),
      ...(opts.uriPrefix?.length ? { uri_prefix: [...opts.uriPrefix] } : {}),
      // Always sent: the server's own default is `all_mine`.
      namespace_scope: opts.namespaceScope ?? "this",
    };
  }

  async _getRelevantDocuments(query: string, _runManager?: CallbackManagerForRetrieverRun): Promise<Document[]> {
    const out = await this.agent.call("knowledge.search", { ...this.body, query } as KnowledgeSearchBody);
    return out.results.map((h) => {
      // A hit names its namespace only on a fan-out; otherwise it is the agent's.
      const namespace = h.namespace || this.agent.namespace;
      return new Document({
        pageContent: h.content,
        metadata: {
          source_uri: h.source_uri,
          chunk_idx: h.chunk_idx,
          similarity: round4(h.similarity),
          section: h.section ?? "",
          namespace,
          tags: [...h.tags],
        },
        id: `${namespace}/${h.source_uri}#${h.chunk_idx}`,
      });
    });
  }
}

/**
 * Memories from `memory.search`, one `Document` per memory. The documents carry no id: a
 * memory key is unique per agent, user and session, and a search reads every agent's memories.
 */
export class LoomMemoryRetriever extends BaseRetriever {
  lc_namespace = ["loom", "retrievers", "memory"];
  private readonly k: number;
  private readonly honourAbstain: boolean;
  private readonly body: Scope & Omit<MemorySearchBody, "query">;

  constructor(
    private readonly agent: Agent,
    opts: LoomMemoryRetrieverOptions = {},
  ) {
    super(opts);
    this.k = opts.k ?? 4;
    this.honourAbstain = opts.honourAbstain ?? true;
    this.body = {
      ...opts.scope,
      top_k: this.k,
      // Above top_k 15 an unpinned search serves 200-character excerpts.
      bodies: "full",
      explain: false,
      ...(opts.searchTags?.length ? { tags: [...opts.searchTags] } : {}),
      ...(opts.memoryType ? { memory_type: opts.memoryType } : {}),
    };
  }

  async _getRelevantDocuments(query: string, _runManager?: CallbackManagerForRetrieverRun): Promise<Document[]> {
    const out = await this.agent.recall(query, this.body);
    if (this.honourAbstain && out.abstain) return [];
    // A response that does not abstain can still carry rows below the floor; honouring abstention
    // drops them. Derived rows may come back above the top_k slice, so cut after filtering.
    const hits = (out.results ?? []).filter((h) => !(this.honourAbstain && h.below_threshold)).slice(0, this.k);
    return hits.map(
      (h) =>
        new Document({
          pageContent: h.content_excerpt,
          metadata: {
            // Named on a fan-out (the server's memory_search_default_namespace_scope).
            namespace: h.namespace || this.agent.namespace,
            memory_key: h.memory_key,
            memory_type: h.memory_type,
            similarity: round4(h.similarity),
            source: h.source ?? "fact", // the server's default, as the Python SDK reads it
            ...(h.valid_at ? { valid_at: h.valid_at } : {}),
          },
        }),
    );
  }
}

function round4(x: number): number {
  return Math.round(x * 10_000) / 10_000;
}
