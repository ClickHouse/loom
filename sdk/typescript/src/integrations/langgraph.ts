/**
 * Loom as a LangGraph.js `BaseStore` (TypeScript twin of
 * `loom.integrations.langgraph.LoomStore`).
 *
 * LangGraph's long-term memory is a key-value store addressed by a namespace
 * tuple and a key, with `put` / `get` / `delete` / `search` (optionally by
 * natural-language `query`) and `listNamespaces`. Every graph node reaches it
 * through the store the graph was compiled with. A `BaseStore` is one class
 * with one abstract method, `batch`, that executes a list of typed operations.
 * This adapter is that class over a Loom `Agent`: a LangGraph namespace becomes
 * a key prefix plus tags, a `put` is `memory.set` (dedup on write, conflict
 * detection, TTL, bitemporal validity), a `search` with a query is
 * `memory.search` (hybrid ranking, calibrated abstention) and without one is
 * `memory.list`, and the tenant boundary is Loom's namespace authorisation:
 * the graph never sees a partition key.
 *
 *     const agent = new Agent("acme", "support", "assistant", { baseUrl, apiKey });
 *     const store = new LoomStore(agent);
 *     const graph = builder.compile({ store });
 *
 *     // inside a node
 *     await store.put(["users", userId], "prefs", { seat: "aisle" });
 *     await store.search(["users", userId], { query: "seating preference", limit: 3 });
 *
 * Mapping (the same keys and tags as the Python store, so a Python graph and a
 * TypeScript graph on one namespace read each other's items):
 *
 * - namespace `["users", "alice"]` + key `"prefs"` is the memory key
 *   `lg/users/alice/prefs`, with tags `lg:users` and `lg:users/alice` (one per
 *   prefix), so a prefix search is one tag filter.
 * - `value` (a JSON object) is stored as the memory's value; `index: false`
 *   becomes `memory_type: "episodic"` (kept, not ranked); a `ttl` on the put
 *   operation (minutes) becomes `ttl_seconds`.
 * - `search({ query })` ranks with `memory.search` (`similarity` is the
 *   `SearchItem.score`) then fetches the values in one `memory.bulk_get`;
 *   `search()` without a query lists by recency. `filter` applies to the stored
 *   value with LangGraph's operators (`$eq $ne $gt $gte $lt $lte`).
 * - `listNamespaces` derives the tuples from the keys under `lg/`.
 *
 * Limits: a ranked search asks for at most `searchTopKCap` (50) rows, so
 * `offset + limit` past it returns fewer items. A search without a query and
 * `listNamespaces` page through `memory.list` until they have what was asked
 * for or the keys run out; `filter` applies to each page read.
 *
 * `@langchain/langgraph-checkpoint` (re-exported by `@langchain/langgraph`) is
 * an optional peer dependency: the class extends its `BaseStore`.
 */

import {
  BaseStore,
  type GetOperation,
  type Item,
  type ListNamespacesOperation,
  type MatchCondition,
  type Operation,
  type OperationResults,
  type PutOperation,
  type SearchItem,
  type SearchOperation,
} from "@langchain/langgraph-checkpoint";
import type { Agent } from "../agent.js";

// ---- key + tag mapping --------------------------------------------------------------

export const KEY_PREFIX = "lg";
export const TAG_PREFIX = "lg:";
const SEP = "/";
/** `memory.list`'s page size cap: the store pages past it with `offset`. */
const LIST_PAGE = 1000;
/** The most rows one ranked search asks the server for, as in the Python store. */
const DEFAULT_SEARCH_TOP_K_CAP = 50;

function checkSegment(seg: unknown, what: string): void {
  if (typeof seg !== "string" || !seg || seg.includes(SEP)) {
    throw new Error(`${what} must be a non-empty string without '/': ${JSON.stringify(seg)}`);
  }
}

/** `["users", "alice"]` + `"prefs"` gives `lg/users/alice/prefs`. */
export function memoryKeyFor(namespace: readonly string[], key: string): string {
  for (const seg of namespace) checkSegment(seg, "namespace segment");
  checkSegment(key, "key");
  return [KEY_PREFIX, ...namespace, key].join(SEP);
}

/** Inverse of {@link memoryKeyFor}; `null` for a key this adapter did not write. */
export function parseMemoryKey(memoryKey: string): { namespace: string[]; key: string } | null {
  const parts = memoryKey.split(SEP);
  if (parts.length < 3 || parts[0] !== KEY_PREFIX || parts.some((p) => !p)) return null;
  return { namespace: parts.slice(1, -1), key: parts[parts.length - 1] };
}

/** One tag per prefix, so a prefix search is one tag: `["users", "alice"]` gives `["lg:users", "lg:users/alice"]`. */
export function tagsFor(namespace: readonly string[]): string[] {
  return namespace.map((_, i) => TAG_PREFIX + namespace.slice(0, i + 1).join(SEP));
}

/** The one tag that selects a namespace prefix; `undefined` for the empty prefix. */
export function prefixTag(namespacePrefix: readonly string[]): string | undefined {
  return namespacePrefix.length ? TAG_PREFIX + namespacePrefix.join(SEP) : undefined;
}

// ---- filters (LangGraph's operators, applied to the stored value) ---------------------

type Comparator = (have: unknown, want: unknown) => boolean;

const compare = (fn: (a: number | string, b: number | string) => boolean): Comparator => {
  return (have, want) => {
    if (have === null || have === undefined) return false;
    if (typeof have !== typeof want || (typeof have !== "number" && typeof have !== "string")) return false;
    return fn(have, want as number | string);
  };
};

const OPS: Record<string, Comparator> = {
  $eq: (a, b) => deepEqual(a, b),
  $ne: (a, b) => !deepEqual(a, b),
  $gt: compare((a, b) => a > b),
  $gte: compare((a, b) => a >= b),
  $lt: compare((a, b) => a < b),
  $lte: compare((a, b) => a <= b),
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => deepEqual(x, b[i]));
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    return ka.length === Object.keys(b).length && ka.every((k) => k in b && deepEqual(a[k], b[k]));
  }
  return false;
}

/** `{ kind: "pref" }` is equality; `{ n: { $gt: 3 } }` is an operator. */
export function matchesFilter(value: Record<string, unknown>, filter?: Record<string, unknown> | null): boolean {
  if (!filter) return true;
  for (const [field, want] of Object.entries(filter)) {
    const have = value[field];
    if (isPlainObject(want) && Object.keys(want).length && Object.keys(want).every((k) => k.startsWith("$"))) {
      for (const [op, arg] of Object.entries(want)) {
        const fn = OPS[op];
        if (!fn) throw new Error(`unsupported filter operator ${JSON.stringify(op)}`);
        if (!fn(have, arg)) return false;
      }
    } else if (!deepEqual(have, want)) {
      return false;
    }
  }
  return true;
}

function pathMatches(have: readonly string[], want: readonly string[]): boolean {
  return have.length === want.length && want.every((w, i) => w === "*" || w === have[i]);
}

function matchNamespace(ns: readonly string[], conds?: readonly MatchCondition[]): boolean {
  for (const c of conds ?? []) {
    const path = c.path;
    if (c.matchType === "prefix" && !pathMatches(ns.slice(0, path.length), path)) return false;
    if (c.matchType === "suffix" && !pathMatches(path.length ? ns.slice(ns.length - path.length) : [], path)) {
      return false;
    }
  }
  return true;
}

function toDate(v: unknown): Date {
  if (typeof v === "string" && v) {
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) return d;
  }
  return new Date();
}

/** LangGraph values are JSON objects; a scalar Loom value is wrapped. */
function asRecord(value: unknown): Record<string, unknown> {
  return isPlainObject(value) ? { ...value } : { value };
}

/** Compare namespaces the way Python's `sorted` orders tuples of strings (by code point). */
function compareNamespaces(a: readonly string[], b: readonly string[]): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return a.length - b.length;
}

// ---- the store ----------------------------------------------------------------------

export interface LoomStoreOptions {
  /** Extra tags on every write (a project, a tenant label). */
  tags?: string[];
  /** The most rows one ranked search asks the server for. Default 50. */
  searchTopKCap?: number;
}

type RankedKey = readonly [memoryKey: string, score: number | undefined];

/** A put operation as LangGraph.js may send it: newer versions carry a TTL in minutes. */
type PutWithTtl = PutOperation & { ttl?: number | null };

function isGet(op: Operation): op is GetOperation {
  return "namespace" in op && "key" in op && !("value" in op);
}
function isPut(op: Operation): op is PutOperation {
  return "namespace" in op && "key" in op && "value" in op;
}
function isSearch(op: Operation): op is SearchOperation {
  return "namespacePrefix" in op;
}
function isListNamespaces(op: Operation): op is ListNamespacesOperation {
  return "limit" in op && "offset" in op && !("namespacePrefix" in op);
}

/** A LangGraph.js `BaseStore` over a Loom `Agent` (see the module comment). */
export class LoomStore extends BaseStore {
  readonly supportsTtl = true;
  private readonly tags: readonly string[];
  private readonly cap: number;

  constructor(
    private readonly agent: Agent,
    opts: LoomStoreOptions = {},
  ) {
    super();
    this.tags = [...(opts.tags ?? [])];
    this.cap = Math.trunc(opts.searchTopKCap ?? DEFAULT_SEARCH_TOP_K_CAP);
  }

  /** Runs every operation in order and returns one result per operation (`undefined` for a put). */
  async batch<Op extends Operation[]>(operations: Op): Promise<OperationResults<Op>> {
    const out: unknown[] = [];
    for (const op of operations) {
      if (isSearch(op)) out.push(await this.search_(op));
      else if (isPut(op)) {
        await this.put_(op);
        out.push(undefined);
      } else if (isGet(op)) out.push(await this.get_(op));
      else if (isListNamespaces(op)) out.push(await this.listNamespaces_(op));
      else throw new TypeError("unsupported store operation");
    }
    return out as OperationResults<Op>;
  }

  // -- one op each --

  private async get_(op: GetOperation): Promise<Item | null> {
    const row = await this.agent.call("memory.get", { memory_key: memoryKeyFor(op.namespace, op.key) });
    if (!row || row.found === false) return null;
    return {
      value: asRecord(row.value),
      key: op.key,
      namespace: [...op.namespace],
      createdAt: toDate(row.created_at),
      updatedAt: toDate(row.updated_at ?? row.created_at),
    };
  }

  private async put_(op: PutWithTtl): Promise<void> {
    const memoryKey = memoryKeyFor(op.namespace, op.key);
    if (op.value === null || op.value === undefined) {
      await this.agent.call("memory.delete", { memory_key: memoryKey });
      return;
    }
    const ttl = op.ttl;
    await this.agent.call("memory.set", {
      memory_key: memoryKey,
      value: { ...op.value },
      tags: [...this.tags, ...tagsFor(op.namespace)],
      // index: false keeps the row but does not rank it (LangGraph's contract)
      memory_type: op.index === false ? "episodic" : "semantic",
      ...(typeof ttl === "number" ? { ttl_seconds: Math.max(1, Math.trunc(ttl * 60)) } : {}),
    });
  }

  private async search_(op: SearchOperation): Promise<SearchItem[]> {
    const prefix = op.namespacePrefix;
    const tag = prefixTag(prefix);
    const offset = Math.max(0, Math.trunc(op.offset ?? 0));
    const limit = Math.trunc(op.limit ?? 10);
    const want = offset + limit;
    if (op.query) {
      const res = await this.agent.call("memory.search", {
        query: op.query,
        ...(tag ? { tags: [tag] } : {}),
        top_k: Math.min(want * 2, this.cap),
        include_tags: false,
      });
      const ranked: RankedKey[] = (res.results ?? [])
        .filter((h) => parseMemoryKey(h.memory_key))
        .map((h) => [h.memory_key, h.similarity] as const);
      return (await this.items(ranked, prefix, op.filter)).slice(offset, want);
    }
    const items: SearchItem[] = [];
    const listPrefix = [KEY_PREFIX, ...prefix].join(SEP) + SEP;
    for await (const keys of this.keyPages(listPrefix, tag ? [tag] : undefined)) {
      items.push(
        ...(await this.items(
          keys.map((k) => [k, undefined] as const),
          prefix,
          op.filter,
        )),
      );
      if (items.length >= want) break;
    }
    return items.slice(offset, want);
  }

  /** The store's memory keys under `prefix`, one `memory.list` page at a time. */
  private async *keyPages(prefix: string, tags?: string[]): AsyncGenerator<string[]> {
    let offset = 0;
    for (;;) {
      const listed = await this.agent.call("memory.list", {
        prefix,
        ...(tags ? { tags } : {}),
        limit: LIST_PAGE,
        offset,
        fields: ["memory_key"],
      });
      const entries = listed.entries ?? [];
      yield entries.map((e) => e.memory_key).filter((k): k is string => typeof k === "string" && !!parseMemoryKey(k));
      if (!listed.truncated || !entries.length) return;
      offset += entries.length;
    }
  }

  /** The `SearchItem`s for `ranked` keys under `prefix` whose value matches `filter`. */
  private async items(
    ranked: readonly RankedKey[],
    prefix: readonly string[],
    filter?: Record<string, unknown>,
  ): Promise<SearchItem[]> {
    if (!ranked.length) return [];
    const fetched = (await this.agent.call("memory.bulk_get", { memory_keys: ranked.map(([k]) => k) })).results ?? {};
    const items: SearchItem[] = [];
    for (const [memoryKey, score] of ranked) {
      const row = fetched[memoryKey];
      const parsed = parseMemoryKey(memoryKey);
      if (!row || row.found === false || !parsed) continue;
      if (!pathMatches(parsed.namespace.slice(0, prefix.length), prefix)) continue;
      const value = asRecord(row.value);
      if (!matchesFilter(value, filter)) continue;
      items.push({
        namespace: parsed.namespace,
        key: parsed.key,
        value,
        createdAt: toDate(row.created_at),
        updatedAt: toDate(row.updated_at ?? row.created_at),
        ...(score === undefined ? {} : { score }),
      });
    }
    return items;
  }

  private async listNamespaces_(op: ListNamespacesOperation): Promise<string[][]> {
    const seen = new Map<string, string[]>();
    for await (const keys of this.keyPages(KEY_PREFIX + SEP)) {
      for (const memoryKey of keys) {
        const parsed = parseMemoryKey(memoryKey);
        if (!parsed || !matchNamespace(parsed.namespace, op.matchConditions)) continue;
        const ns = op.maxDepth === undefined ? parsed.namespace : parsed.namespace.slice(0, op.maxDepth);
        seen.set(JSON.stringify(ns), ns);
      }
    }
    const ordered = [...seen.values()].sort(compareNamespaces);
    return ordered.slice(op.offset, op.offset + op.limit);
  }
}
