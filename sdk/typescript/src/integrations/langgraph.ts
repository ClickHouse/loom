/**
 * Loom as a LangGraph.js `BaseStore` (the TypeScript twin of
 * `loom.integrations.langgraph`).
 *
 * A LangGraph namespace and key become one Loom memory, so graphs written in
 * either language read each other's items:
 *
 * - namespace `["users", "alice"]` + key `"prefs"` is the memory key
 *   `lg/users/alice/prefs`, tagged `lg:users` and `lg:users/alice` (one tag per
 *   prefix) plus the adapter's own tags.
 * - A JSON object is stored as the memory value; any other value is wrapped as
 *   `{ value }`.
 *
 * - `search` with a `query` ranks the bound agent's rows (`score` is the hit's
 *   `similarity`) and then reads their values in one `memory.bulk_get`; without
 *   a query it lists by recency. `filter` applies to the stored value with
 *   `$eq $ne $gt $gte $lt $lte`, and a plain value means equality.
 * - `listNamespaces` derives the namespaces from the keys under `lg/`.
 *
 * Limits: a ranked search asks for at most `searchTopKCap` (50) rows, so
 * `offset + limit` past it returns fewer items. An unranked search and
 * `listNamespaces` page through `memory.list` until they have what was asked
 * for or the keys run out.
 *
 * Identity and isolation come from the Loom `Agent`.
 *
 *     const store = new LoomStore(new Datapack("acme", "support", { baseUrl, apiKey }).agent("assistant"));
 *     const graph = builder.compile({ store });
 *
 * Two options exist only on a direct `store.batch()` put: `index: false` stores
 * an episodic memory, and `ttl` (minutes) expires the item. A compiled graph's
 * store wrapper forwards neither: its puts are always semantic and never expire.
 * The wrapper also inherits upstream `listNamespaces`, which throws, so
 * namespace listing is unavailable from graph nodes; call it on the store.
 */

import {
  BaseStore,
  type GetOperation,
  InvalidNamespaceError,
  type Item,
  type MatchCondition,
  type Operation,
  type OperationResults,
  type PutOperation,
  type SearchItem,
} from "@langchain/langgraph-checkpoint";
import type { Agent, Body } from "../agent.js";
import type { OpName } from "../generated/ops.js";
import { boundProtection, requireAutomaticAllowed, responseRoots, SessionCapture } from "../session-protection.js";
import type { CallOptions } from "../transport.js";

/** A `PutOperation` with the TTL, in minutes, that `BaseStore.put()` cannot carry. */
export interface LoomPutOperation extends PutOperation {
  ttl?: number | null;
}

export interface LoomStoreOptions {
  /** Extra tags on every write (a project, a tenant label). */
  tags?: string[];
  /** App-owned family that makes the store's calls automatic; omit for explicit use. */
  capture?: SessionCapture;
  /** The most rows one ranked search asks the server for (1 to 500, default 50). */
  searchTopKCap?: number;
}

type Scope = Pick<CallOptions, "capture" | "observe">;
type Filter = Record<string, unknown>;
type Step =
  | { kind: "get"; namespace: string[]; key: string }
  | { kind: "delete"; memoryKey: string }
  | { kind: "set"; body: Body<"memory.set"> }
  | { kind: "search"; prefix: string[]; query?: string; filter?: Filter; limit: number; offset: number }
  | { kind: "namespaces"; conditions: MatchCondition[]; maxDepth?: number; limit: number; offset: number };
type Found = { namespace: string[]; key: string; memoryKey: string; score?: number };

const KEY_PREFIX = "lg";
const TAG_PREFIX = "lg:";
const SEP = "/";
// memory.list's page size cap: later pages follow with `offset`.
const LIST_PAGE = 1000;

// `/` is the only delimiter hazard; `.` and `:` stay legal so items Python wrote still read.
function segment(value: unknown, what: string): string {
  if (typeof value !== "string" || !value || value.includes(SEP)) {
    const message = `${what} must be a non-empty string without '/': ${JSON.stringify(value)}`;
    throw what === "key" ? new Error(message) : new InvalidNamespaceError(message);
  }
  return value;
}

function memoryKeyFor(namespace: string[], key: string): string {
  if (!Array.isArray(namespace)) throw new InvalidNamespaceError("namespace must be an array of strings");
  return [KEY_PREFIX, ...namespace.map((s) => segment(s, "namespace segment")), segment(key, "key")].join(SEP);
}

function tagsFor(namespace: string[]): string[] {
  return namespace.map((_, i) => TAG_PREFIX + namespace.slice(0, i + 1).join(SEP));
}

// The inverse of memoryKeyFor; undefined for a key this adapter did not write.
function parseMemoryKey(memoryKey: string): Pick<Found, "namespace" | "key"> | undefined {
  const parts = memoryKey.split(SEP);
  if (parts.length < 3 || parts[0] !== KEY_PREFIX || parts.some((p) => !p)) return undefined;
  return { namespace: parts.slice(1, -1), key: parts[parts.length - 1] };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function equal(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => equal(x, b[i]));
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((k) => Object.hasOwn(b, k) && equal(a[k], b[k]));
  }
  return false;
}

// Numbers order against numbers and strings against strings; any other pair never orders.
function order(a: unknown, b: unknown): number | undefined {
  if (typeof a !== typeof b || (typeof a !== "number" && typeof a !== "string")) return undefined;
  return a < (b as typeof a) ? -1 : a > (b as typeof a) ? 1 : 0;
}

const OPERATORS: Record<string, (have: unknown, want: unknown) => boolean> = {
  $eq: (have, want) => equal(have, want),
  $ne: (have, want) => !equal(have, want),
  $gt: (have, want) => (order(have, want) ?? 0) > 0,
  $gte: (have, want) => (order(have, want) ?? -1) >= 0,
  $lt: (have, want) => (order(have, want) ?? 0) < 0,
  $lte: (have, want) => (order(have, want) ?? 1) <= 0,
};

// `{ kind: "pref" }` is equality; `{ n: { $gt: 3 } }` is operators.
function operatorsOf(want: unknown): [string, unknown][] | undefined {
  const entries = isObject(want) ? Object.entries(want) : [];
  return entries.length && entries.every(([op]) => op.startsWith("$")) ? entries : undefined;
}

function checkFilter(filter: unknown): Filter | undefined {
  if (filter === undefined || filter === null) return undefined;
  if (!isObject(filter)) throw new Error("search filter must be an object");
  for (const want of Object.values(filter)) {
    for (const [op] of operatorsOf(want) ?? []) {
      if (!Object.hasOwn(OPERATORS, op)) throw new Error(`unsupported filter operator ${JSON.stringify(op)}`);
    }
  }
  return filter;
}

// A comparison against a missing field is false; equality treats it as null.
function matchesFilter(value: Record<string, unknown>, filter: Filter | undefined): boolean {
  return Object.entries(filter ?? {}).every(([field, want]) => {
    const have = Object.hasOwn(value, field) ? value[field] : null;
    const operators = operatorsOf(want);
    return operators ? operators.every(([op, arg]) => OPERATORS[op](have, arg)) : equal(have, want);
  });
}

function count(value: unknown, fallback: number, what: string): number {
  const n = value ?? fallback;
  if (typeof n !== "number" || !Number.isInteger(n) || n < 0) {
    throw new Error(`${what} must be a non-negative integer: ${JSON.stringify(value)}`);
  }
  return n;
}

// `*` stands for any one segment; a path longer than the namespace never matches.
function matchesPath({ matchType, path }: MatchCondition, namespace: string[]): boolean {
  if (path.length > namespace.length) return false;
  const have =
    matchType === "prefix" ? namespace.slice(0, path.length) : namespace.slice(namespace.length - path.length);
  return path.every((want, i) => want === "*" || want === have[i]);
}

function compareNamespaces(a: string[], b: string[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return a.length - b.length;
}

function asObject(value: unknown): Record<string, unknown> {
  return isObject(value) ? value : { value: value ?? null };
}

// A timestamp without a zone is UTC, as in the Python adapter.
function parseDate(value: unknown): Date | undefined {
  if (typeof value === "string" && value) {
    const iso = value.replace(" ", "T");
    const date = new Date(/(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
    if (!Number.isNaN(date.getTime())) return date;
  }
  return undefined;
}

function toDate(value: unknown): Date {
  return parseDate(value) ?? new Date();
}

function isExpired(expiresAt: string | null | undefined, now: number): boolean {
  const expiry = parseDate(expiresAt);
  return expiry !== undefined && expiry.getTime() <= now;
}

function ttlSeconds(ttl: unknown): number | undefined {
  if (ttl === undefined || ttl === null) return undefined;
  if (typeof ttl !== "number" || !Number.isFinite(ttl) || ttl <= 0) {
    throw new Error(`ttl must be a positive number of minutes: ${JSON.stringify(ttl)}`);
  }
  return Math.max(1, Math.floor(ttl * 60));
}

export class LoomStore extends BaseStore {
  private readonly agent: Agent;
  private readonly tags: string[];
  private readonly capture?: SessionCapture;
  private readonly topKCap: number;
  private observers?: SessionCapture;

  constructor(agent: Agent, options: LoomStoreOptions = {}) {
    super();
    this.agent = agent;
    this.tags = [...(options.tags ?? [])];
    this.topKCap = options.searchTopKCap ?? 50;
    if (!Number.isInteger(this.topKCap) || this.topKCap < 1 || this.topKCap > 500) {
      throw new RangeError(`searchTopKCap must be an integer from 1 to 500: ${options.searchTopKCap}`);
    }
    this.capture = options.capture === undefined ? undefined : new SessionCapture(options.capture.roots);
    const roots = responseRoots(agent.client.transport.sessionProtection, this.capture);
    this.observers = roots.length ? new SessionCapture(roots) : undefined;
  }

  /** Results align with `operations`. An invalid operation rejects before any request is sent. */
  override async batch<Op extends (Operation | LoomPutOperation)[]>(operations: Op): Promise<OperationResults<Op>> {
    // Retain the callback's families and read every operation before the first await.
    const scope = this.scope();
    const steps = [...operations].map((op) => this.plan(op));
    const results: unknown[] = [];
    for (const step of steps) results.push(await this.run(step, scope));
    return results as OperationResults<Op>;
  }

  /**
   * `BaseStore.listNamespaces` without its namespace validation, so prefixes
   * the Python adapter wrote (with `.` or `:`) list too. A compiled graph's
   * store wrapper inherits the upstream method, which throws, so this is
   * unavailable from graph nodes.
   */
  override async listNamespaces(
    options: { prefix?: string[]; suffix?: string[]; maxDepth?: number; limit?: number; offset?: number } = {},
  ): Promise<string[][]> {
    const { prefix, suffix, maxDepth, limit = 100, offset = 0 } = options;
    const matchConditions: MatchCondition[] = [];
    if (prefix) matchConditions.push({ matchType: "prefix", path: prefix });
    if (suffix) matchConditions.push({ matchType: "suffix", path: suffix });
    const op = { matchConditions: matchConditions.length ? matchConditions : undefined, maxDepth, limit, offset };
    return (await this.batch([op]))[0];
  }

  private scope(): Scope {
    const roots = responseRoots(this.agent.client.transport.sessionProtection, this.capture, this.observers);
    if (!roots.length) return {};
    const retained = new SessionCapture(roots);
    this.observers = retained;
    return this.capture ? { capture: retained } : { observe: retained };
  }

  private async allowed(scope: Scope): Promise<void> {
    if (!scope.capture) return;
    const roots = scope.capture.roots;
    await boundProtection(requireAutomaticAllowed(roots, true), roots, this.agent.client.transport.timeoutMs);
  }

  private plan(op: Operation | LoomPutOperation): Step {
    if ("namespacePrefix" in op) {
      if (!Array.isArray(op.namespacePrefix)) throw new InvalidNamespaceError("namespacePrefix must be an array");
      if (op.query !== undefined && op.query !== null && typeof op.query !== "string") {
        throw new Error("search query must be a string");
      }
      return {
        kind: "search",
        prefix: op.namespacePrefix.map((s) => segment(s, "namespace segment")),
        query: op.query ?? undefined,
        filter: checkFilter(op.filter),
        limit: count(op.limit, 10, "search limit"),
        offset: count(op.offset, 0, "search offset"),
      };
    }
    if ("value" in op) {
      const memoryKey = memoryKeyFor(op.namespace, op.key);
      if (op.value === null) return { kind: "delete", memoryKey };
      if (op.namespace.length === 0) {
        throw new InvalidNamespaceError("LoomStore writes need a non-empty namespace");
      }
      const seconds = ttlSeconds((op as LoomPutOperation).ttl);
      return {
        kind: "set",
        body: {
          memory_key: memoryKey,
          value: asObject(op.value),
          tags: [...this.tags, ...tagsFor(op.namespace)],
          // index: false keeps the row without ranking it, as in LangGraph's contract.
          memory_type: op.index === false ? "episodic" : "semantic",
          ...(seconds === undefined ? {} : { ttl_seconds: seconds }),
        },
      };
    }
    if ("key" in op && "namespace" in op) {
      memoryKeyFor(op.namespace, op.key);
      return { kind: "get", namespace: [...(op as GetOperation).namespace], key: op.key };
    }
    if ("limit" in op && "offset" in op) {
      const conditions = op.matchConditions ?? [];
      for (const c of conditions) {
        if ((c?.matchType !== "prefix" && c?.matchType !== "suffix") || !Array.isArray(c.path)) {
          throw new Error(`unsupported namespace match condition: ${JSON.stringify(c)}`);
        }
        if (c.path.some((s) => typeof s !== "string")) throw new Error("match condition paths hold strings");
      }
      return {
        kind: "namespaces",
        conditions,
        maxDepth: op.maxDepth === undefined || op.maxDepth === null ? undefined : count(op.maxDepth, 0, "maxDepth"),
        limit: count(op.limit, 100, "listNamespaces limit"),
        offset: count(op.offset, 0, "listNamespaces offset"),
      };
    }
    throw new Error("LoomStore does not support this store operation");
  }

  private send<K extends OpName>(name: K, body: Body<K>, scope: Scope) {
    return this.agent.call(name, body, {}, scope);
  }

  private async run(step: Step, scope: Scope): Promise<Item | SearchItem[] | string[][] | null> {
    let result: Item | SearchItem[] | string[][] | null = null;
    if (step.kind === "get") {
      const out = await this.send("memory.get", { memory_key: memoryKeyFor(step.namespace, step.key) }, scope);
      result = out.found
        ? {
            value: asObject(out.value),
            key: step.key,
            namespace: step.namespace,
            createdAt: toDate(out.created_at),
            updatedAt: toDate(out.updated_at ?? out.created_at),
          }
        : null;
    } else if (step.kind === "delete") {
      await this.send("memory.delete", { memory_key: step.memoryKey }, scope);
    } else if (step.kind === "set") {
      await this.send("memory.set", step.body, scope);
    } else if (step.kind === "search") {
      result = await this.runSearch(step, scope);
    } else {
      result = await this.runNamespaces(step, scope);
    }
    // The reply may have protected the family: release nothing the graph could keep.
    await this.allowed(scope);
    return result;
  }

  // The store's memory keys under `prefix`, one `memory.list` page at a time.
  private async *keyPages(prefix: string, tags: string[], scope: Scope): AsyncGenerator<string[]> {
    for (let offset = 0; ; ) {
      const page = await this.send(
        "memory.list",
        { prefix, ...(tags.length ? { tags } : {}), limit: LIST_PAGE, offset, fields: ["memory_key", "expires_at"] },
        scope,
      );
      const entries = page.entries ?? [];
      // Loom can still list expired rows until the sweep. Advance by the raw page size.
      const now = Date.now();
      yield entries.filter((e) => !isExpired(e.expires_at, now)).map((e) => e.memory_key);
      if (!page.truncated || !entries.length) return;
      offset += entries.length;
    }
  }

  // The search items for keys this adapter wrote under `prefix` whose value matches `filter`,
  // in the order given; one bulk read for all of them.
  private async items(found: Found[], filter: Filter | undefined, scope: Scope): Promise<SearchItem[]> {
    if (!found.length) return [];
    const { results = {} } = await this.send("memory.bulk_get", { memory_keys: found.map((f) => f.memoryKey) }, scope);
    const items: SearchItem[] = [];
    const now = Date.now();
    for (const { namespace, key, memoryKey, score } of found) {
      const row = results[memoryKey];
      if (!row?.found || isExpired(row.expires_at, now)) continue;
      const value = asObject(row.value);
      if (!matchesFilter(value, filter)) continue;
      items.push({
        namespace,
        key,
        value,
        createdAt: toDate(row.created_at),
        updatedAt: toDate(row.updated_at ?? row.created_at),
        ...(score === undefined ? {} : { score }),
      });
    }
    return items;
  }

  private async runSearch(step: Extract<Step, { kind: "search" }>, scope: Scope): Promise<SearchItem[]> {
    const { prefix, query, filter, limit, offset } = step;
    const want = offset + limit;
    if (!limit) return [];
    const under = (keys: [string, number | undefined][]): Found[] =>
      keys.flatMap(([memoryKey, score]) => {
        const parsed = parseMemoryKey(memoryKey);
        return parsed && prefix.every((s, i) => parsed.namespace[i] === s) ? [{ ...parsed, memoryKey, score }] : [];
      });
    const tags = tagsFor(prefix).slice(-1);
    if (query) {
      const { results } = await this.send(
        "memory.search",
        {
          query,
          ...(tags.length ? { tags } : {}),
          top_k: Math.min(2 * want, this.topKCap),
          include_tags: false,
          scope: "private",
          namespace_scope: "this", // Match the namespace used by memory.bulk_get.
        },
        scope,
      );
      const ranked = under(results.map((h) => [h.memory_key, h.similarity]));
      return (await this.items(ranked, filter, scope)).slice(offset, want);
    }
    const items: SearchItem[] = [];
    for await (const keys of this.keyPages([KEY_PREFIX, ...prefix].join(SEP) + SEP, tags, scope)) {
      items.push(...(await this.items(under(keys.map((k) => [k, undefined])), filter, scope)));
      if (items.length >= want) break;
    }
    return items.slice(offset, want);
  }

  private async runNamespaces(step: Extract<Step, { kind: "namespaces" }>, scope: Scope): Promise<string[][]> {
    const seen = new Map<string, string[]>();
    for await (const keys of this.keyPages(KEY_PREFIX + SEP, [], scope)) {
      for (const memoryKey of keys) {
        const parsed = parseMemoryKey(memoryKey);
        if (!parsed || !step.conditions.every((c) => matchesPath(c, parsed.namespace))) continue;
        const namespace = parsed.namespace.slice(0, step.maxDepth);
        seen.set(namespace.join(SEP), namespace);
      }
    }
    return [...seen.values()].sort(compareNamespaces).slice(step.offset, step.offset + step.limit);
  }
}
