/**
 * `LoomClient` — every wire operation the server declares, typed through the
 * generated `OpMap`.
 *
 *     const loom = new LoomClient({ baseUrl, apiKey });
 *     const out = await loom.call("memory.search", { org, namespace, query: "deploy window" });
 *     // out: MemorySearchOutput
 *
 * Group accessors are generated from the op names: `loom.memory.search(input)`
 * is `loom.call("memory.search", input)`, `loom.knowledge.bulkAdd(...)` is
 * `knowledge.bulk_add`, and so on for every group. Both forms are the same
 * call; the dotted name is the one that appears in the server's ledger.
 */

import { type Capabilities, capabilities as fetchCapabilities } from "./capabilities.js";
import { OP_BY_NAME, OPS, type OpMap, type OpName, type OpSpec } from "./generated/ops.js";
import { envVar } from "./runtime.js";
import { type CallOptions, type CallResult, Transport, type TransportOptions } from "./transport.js";

export type OpInput<K extends OpName> = OpMap[K]["input"];
export type OpOutput<K extends OpName> = OpMap[K]["output"];

/**
 * The camelCase method name of a dotted op, as the generated `OPS[].method` spells it:
 * `memory.bulk_set` → `bulkSet`, `tracker.source.add` → `sourceAdd`.
 */
export type MethodOf<K extends OpName> = K extends `${string}.${infer M}` ? CamelCase<M> : never;
type CamelCase<S extends string> = S extends `${infer A}_${infer B}`
  ? `${A}${Capitalize<CamelCase<B>>}`
  : S extends `${infer A}.${infer B}`
    ? `${A}${Capitalize<CamelCase<B>>}`
    : S;
export type GroupOf<K extends OpName> = K extends `${infer G}.${string}` ? G : never;
export type Groups = GroupOf<OpName>;
type OpsInGroup<G extends string> = Extract<OpName, `${G}.${string}`>;

/** `loom.memory.search(input)` — one method per op in the group. */
export type GroupClient<G extends string> = {
  [K in OpsInGroup<G> as MethodOf<K>]: (input: OpInput<K>, opts?: CallOptions) => Promise<OpOutput<K>>;
};

export type LoomClientOptions = TransportOptions;

// biome-ignore lint/suspicious/noUnsafeDeclarationMerging: the constructor defines every group property the interface declares.
export class LoomClient {
  readonly transport: Transport;
  private caps: { promise: Promise<Capabilities>; at: number } | undefined;

  constructor(opts: LoomClientOptions | Transport) {
    this.transport = opts instanceof Transport ? opts : new Transport(readEnv(opts));
    for (const group of new Set(OPS.map((o) => o.group))) {
      Object.defineProperty(this, group, {
        value: this.group(group as Groups),
        enumerable: false,
        writable: false,
      });
    }
  }

  /** Call one operation by its dotted name; input and output are typed by `OpMap`. */
  call<K extends OpName>(name: K, input: OpInput<K>, opts?: CallOptions): Promise<OpOutput<K>> {
    return this.transport.post<OpOutput<K>>(name, input, opts);
  }

  /**
   * `call`, returning the response metadata with the body:
   * `requestId` finds the call in the server's ledger, `schemaVersion`,
   * `serverTimingMs` and `rateLimit` are what the server reported.
   */
  callWithResponse<K extends OpName>(name: K, input: OpInput<K>, opts?: CallOptions): Promise<CallResult<OpOutput<K>>> {
    return this.transport.postWithMeta<OpOutput<K>>(name, input, opts);
  }

  /** Call an op the generated table does not know (a newer server): untyped, no idempotency key. */
  callRaw<T = unknown>(name: string, input: unknown, opts?: CallOptions): Promise<T> {
    return this.transport.post<T>(name, input, opts);
  }

  /**
   * What the deployment serves and what this token may call,
   * fetched once and reused for `maxAgeMs` (default 60 s); `{ maxAgeMs: 0 }` refetches.
   */
  capabilities(opts: { maxAgeMs?: number } = {}): Promise<Capabilities> {
    const maxAge = opts.maxAgeMs ?? 60_000;
    const now = Date.now();
    if (!this.caps || maxAge <= 0 || now - this.caps.at > maxAge) {
      const promise = fetchCapabilities(this);
      this.caps = { promise, at: now };
      promise.catch(() => {
        if (this.caps?.promise === promise) this.caps = undefined;
      });
    }
    return this.caps.promise;
  }

  /** The declared spec of an op (role, idempotency, maturity, budget), or undefined. */
  spec(name: string): OpSpec | undefined {
    return OP_BY_NAME[name as OpName];
  }

  /** A typed accessor for one group: `loom.group("memory").search(...)`. */
  group<G extends Groups>(name: G): GroupClient<G> {
    const target: Record<string, unknown> = {};
    for (const spec of OPS) {
      if (spec.group !== name) continue;
      target[spec.method] = (input: unknown, opts?: CallOptions) => this.transport.post(spec.name, input, opts);
    }
    return target as GroupClient<G>;
  }
}

/** The generated groups as properties: `loom.memory`, `loom.knowledge`, … */
export interface LoomClient extends LoomClientGroups {}
export type LoomClientGroups = { readonly [G in Groups]: GroupClient<G> };

/** `LOOM_BASE_URL` / `LOOM_API_KEY` fill what the options leave out. */
export function readEnv(opts: LoomClientOptions): LoomClientOptions {
  const baseUrl = (opts.baseUrl || envVar("LOOM_BASE_URL") || "").trim();
  // A blank value is unset: `LOOM_API_KEY=""` in a shell profile counts as no key.
  const apiKey = [opts.apiKey, envVar("LOOM_API_KEY")].map((v) => v?.trim()).find((v) => v) || undefined;
  if (!baseUrl) throw new Error("LoomClient: baseUrl is required (or set LOOM_BASE_URL)");
  return { ...opts, baseUrl, apiKey };
}
