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
 * `loom.knowledge` also uploads a file and follows its ingest job
 * (`upload`, `waitForIngest`, `ingestFile`; see `files.ts`).
 */

import { AccountClient } from "./account.js";
import { type Capabilities, capabilities as fetchCapabilities } from "./capabilities.js";
import { DEFAULT_SERVER, LocalCredential, StoredLogin, sameServer, signedInServer } from "./credentials.js";
import { AuthError, LoomError } from "./errors.js";
import { type KnowledgeFiles, knowledgeFiles } from "./files.js";
import { OP_BY_NAME, OPS, type OpMap, type OpName, type OpSpec } from "./generated/ops.js";
import { machineLogin } from "./machine.js";
import { envVar, isBrowser } from "./runtime.js";
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

export interface LoomClientOptions extends Omit<TransportOptions, "baseUrl"> {
  /** The server. Default `LOOM_BASE_URL`, else `http://localhost:7777`. */
  baseUrl?: string;
  /**
   * The `loom login` session to use, on the server it signed in to; not with
   * `apiKey` or another `baseUrl`. Needs Node 20.16 or later (22.3 or later
   * on Node 22): the constructor reads the profile's server synchronously
   * with `process.getBuiltinModule`, since the client's `baseUrl` is known
   * from the start.
   */
  profile?: string;
}

// biome-ignore lint/suspicious/noUnsafeDeclarationMerging: the constructor defines every group property the interface declares.
export class LoomClient {
  readonly transport: Transport;
  /** Your own tokens: `loom.account.createToken(...)`, outside the op registry. */
  readonly account: AccountClient;
  private caps: { promise: Promise<Capabilities>; at: number } | undefined;

  constructor(opts: LoomClientOptions | Transport = {}) {
    this.transport = opts instanceof Transport ? opts : new Transport(readEnv(opts));
    this.account = new AccountClient(this.transport);
    for (const group of new Set(OPS.map((o) => o.group))) {
      const ops = this.group(group as Groups);
      Object.defineProperty(this, group, {
        // `loom.knowledge` also sends files (`files.ts`); `group("knowledge")` is the ops alone.
        value: group === "knowledge" ? Object.assign(ops, knowledgeFiles(this.transport)) : ops,
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

  /** A typed accessor for one group's ops: `loom.group("memory").search(...)`. */
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
export type LoomClientGroups = { readonly [G in Exclude<Groups, "knowledge">]: GroupClient<G> } & {
  readonly knowledge: GroupClient<"knowledge"> & KnowledgeFiles;
};

/**
 * The server and credential a client uses, as the Python SDK picks them. A
 * `profile` names the `loom login` session to use, on the server it signed
 * in to. Otherwise the server is `baseUrl`, else `LOOM_BASE_URL`, else
 * localhost, and the credential, first found wins: `tokenProvider`,
 * `apiKey`, `LOOM_API_KEY`, a bot's `LOOM_CLIENT_ID`, then (Node only,
 * decided on the first request) the stored login of `LOOM_PROFILE` (else
 * `default`) for that server and `~/.loom/token`.
 */
export function readEnv(opts: LoomClientOptions = {}): TransportOptions {
  const { profile, ...rest } = opts;
  if (profile) return withProfile(rest, profile);
  const baseUrl = (opts.baseUrl || envVar("LOOM_BASE_URL") || "").trim() || DEFAULT_SERVER;
  if (opts.tokenProvider) return { ...rest, baseUrl };
  // A blank value is unset: `LOOM_API_KEY=""` in a shell profile counts as no key.
  const apiKey = [opts.apiKey, envVar("LOOM_API_KEY")].map((v) => v?.trim()).find((v) => v) || undefined;
  if (apiKey) return { ...rest, baseUrl, apiKey };
  const bot = machineLogin(baseUrl, { fetch: opts.fetch, now: opts.now });
  // A browser has no files to read a stored login or token from.
  const local = bot ?? (isBrowser() ? undefined : new LocalCredential(baseUrl, { fetch: opts.fetch }));
  return { ...rest, baseUrl, apiKey, tokenProvider: local };
}

/** The `loom login` session `profile` names, on the server it signed in to. */
function withProfile(opts: Omit<LoomClientOptions, "profile">, profile: string): TransportOptions {
  if (isBrowser()) throw new LoomError("the `profile` option needs Node: a browser has no stored login");
  const server = signedInServer(profile);
  if (server === undefined) throw new AuthError(`not signed in (profile '${profile}'); run \`loom login\``);
  if (opts.apiKey?.trim() || opts.tokenProvider || (opts.baseUrl && !sameServer(opts.baseUrl, server)))
    throw new LoomError(`profile '${profile}' is a session for ${server}; pass one or the other`);
  return { ...opts, baseUrl: server, apiKey: undefined, tokenProvider: new StoredLogin(profile, server, opts) };
}
