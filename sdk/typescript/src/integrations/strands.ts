/**
 * loom as a Strands Agents `MemoryStore` (TypeScript twin of
 * `loom.integrations.strands.LoomMemoryStore`).
 *
 * Strands' `MemoryManager` consumes any object with the store fields and
 * `search()` (plus `add()` when writable) and folds the results into the
 * model input. This adapter gives a Strands agent loom's memory — dedup on
 * write, conflict detection, bitemporal validity, calibrated abstention —
 * through the SDK's own `Agent`. No dependency on `@strands-agents/sdk`: the
 * protocol shape is structural.
 *
 *     const store = new LoomMemoryStore(agent, { tags: ["customer"] });
 *     // new MemoryManager({ stores: [store] })
 *
 * Identity (org, namespace, agent) comes from the loom `Agent`; the Strands
 * side never sees a partition key.
 */

import type { Agent, Scope } from "../agent.js";
import { LoomError } from "../errors.js";
import type { MemorySearchInput } from "../generated/ops.js";
import { trimSlashes } from "../slashes.js";

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

/** Strands' `MemoryEntry`. */
export interface MemoryEntry {
  content: string;
  storeName?: string;
  metadata?: Record<string, JsonValue>;
}

/** Strands' `SearchOptions`. */
export interface SearchOptions {
  maxSearchResults?: number;
}

export type StrandsMemoryType = NonNullable<MemorySearchInput["memory_type"]>;

export interface LoomMemoryStoreOptions {
  /** The type every write carries and every search is limited to. Default `semantic`. */
  memoryType?: StrandsMemoryType;
  /** Tags stamped on every write; searches are limited to memories carrying them. */
  tags?: string[];
  /** Results per search when the caller does not say. Default 3. */
  maxSearchResults?: number;
  /** Return nothing when loom abstains (no hit above its calibrated floor), and leave out hits below it. Default true. */
  honourAbstain?: boolean;
  /** Key prefix; the key is `<prefix>/<sha256(content) first 20 hex>`, as in the Python adapter. Default `strands`. */
  keyPrefix?: string;
  /** Per-store identity scope (user / session / run). */
  scope?: Scope;
  name?: string;
  description?: string;
}

/** Metadata keys whose short string values become `key:value` tags on write. */
const META_KEYS_KEPT = ["kind", "source", "topic", "category"] as const;

export class LoomMemoryStore {
  readonly name: string;
  readonly description: string;
  readonly maxSearchResults: number;
  readonly writable = true;
  private readonly memoryType: StrandsMemoryType;
  private readonly tags: readonly string[];
  private readonly honourAbstain: boolean;
  private readonly keyPrefix: string;
  private readonly scope: Scope;

  constructor(
    private readonly agent: Agent,
    opts: LoomMemoryStoreOptions = {},
  ) {
    this.name = opts.name ?? "loom";
    this.description = opts.description ?? "Long-term memories in loom, searched by meaning with abstention";
    this.maxSearchResults = opts.maxSearchResults ?? 3;
    this.memoryType = opts.memoryType ?? "semantic";
    this.tags = [...(opts.tags ?? [])];
    this.honourAbstain = opts.honourAbstain ?? true;
    this.keyPrefix = trimSlashes(opts.keyPrefix ?? "strands") || "strands";
    this.scope = opts.scope ?? {};
  }

  /** One `memory.set` under a content-derived key, so adding the same fact twice writes one row. */
  async add(content: string, metadata?: Record<string, JsonValue>): Promise<void> {
    const text = content.trim();
    if (!text) return;
    const tags = [...this.tags];
    for (const k of META_KEYS_KEPT) {
      const v = metadata?.[k];
      if (typeof v === "string" && v && v.length <= 64) tags.push(`${k}:${v}`);
    }
    await this.agent.remember(`${this.keyPrefix}/${await sha256Hex20(text)}`, text, {
      ...this.scope,
      memory_type: this.memoryType,
      ...(tags.length ? { tags } : {}),
    });
  }

  /** `memory.search` limited to this store's tags and type; empty when loom abstains, and without hits below the floor, unless `honourAbstain` is false. */
  async search(query: string, options: SearchOptions = {}): Promise<MemoryEntry[]> {
    const topK = options.maxSearchResults ?? this.maxSearchResults;
    const out = await this.agent.recall(query, {
      ...this.scope,
      top_k: topK,
      // Above top_k 15 an unpinned search serves 200-character excerpts.
      bodies: "full",
      explain: false,
      memory_type: this.memoryType,
      ...(this.tags.length ? { tags: [...this.tags] } : {}),
    });
    if (this.honourAbstain && out.abstain) return [];
    // A response that does not abstain can still carry rows below the floor; honouring abstention
    // drops them. Derived rows may come back above the top_k slice, so cut after filtering.
    const hits = (out.results ?? []).filter((h) => !(this.honourAbstain && h.below_threshold)).slice(0, topK);
    return hits.map((h) => ({
      content: h.content_excerpt,
      storeName: this.name,
      metadata: {
        memory_key: h.memory_key,
        memory_type: h.memory_type,
        similarity: Math.round(h.similarity * 10_000) / 10_000,
        ...(h.source ? { source: h.source } : {}),
        ...(h.valid_at ? { valid_at: h.valid_at } : {}),
      },
    }));
  }
}

async function sha256Hex20(text: string): Promise<string> {
  const subtle = (globalThis as { crypto?: { subtle?: SubtleCrypto } }).crypto?.subtle;
  // A weaker hash would let two facts collide on one key and overwrite each other.
  if (!subtle) throw new LoomError("LoomMemoryStore needs crypto.subtle (a secure context) to key its writes");
  const buf = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 20);
}
