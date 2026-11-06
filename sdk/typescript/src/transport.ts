/**
 * The wire: `POST {baseUrl}/v1/{op}` with a JSON body.
 *
 * Mirrors the Python SDK's `Transport`: connect failures and 5xx are retried
 * with exponential backoff (the server did not process the request); a 429 is
 * retried after the server's `Retry-After` for at most `ratelimitMaxWaitS` per
 * call before `RateLimited` surfaces; a read timeout is NOT retried (the server
 * may still be processing). The per-call timeout and the caller's `signal`
 * cover the whole exchange, body included, and every sleep between attempts:
 * an aborted signal sends nothing more. Idempotent ops that are not read-only
 * get an `Idempotency-Key` unless the caller supplied one (the server keeps no
 * replay of a read-only op); every call carries `X-Request-Id` so a failure can be
 * found in the server's ledger. Response `X-RateLimit-*` headers are read so
 * a client at the floor paces itself before the next request.
 *
 * The credential lives in an ECMAScript private field, so
 * `JSON.stringify(transport)` and `util.inspect` never carry it and a blank
 * key is no key; every request identifies the SDK (`User-Agent`,
 * `X-Loom-SDK`, `X-Loom-Runtime`, `X-Loom-Retry-Count`); `Retry-After` is
 * parsed as seconds or an HTTP-date; `postWithMeta` returns the response
 * metadata (`CallResult`) alongside the body.
 *
 * `get` reads a path outside the op registry (`me/service_accounts`) the same
 * way: same credential, retries and errors, no body and no Idempotency-Key.
 *
 * Instead of a static `apiKey`, a `tokenProvider` (a bot's `MachineLogin`, a
 * `loom login` session's `StoredLogin`) hands over the token for each
 * request; a 401 is sent once more with a fresh one, and the token never goes
 * to an origin other than the provider's. A provider's empty token sends no
 * Authorization header.
 */

import {
  AuthError,
  BackendUnavailable,
  errorFromResponse,
  INVALID_RESPONSE_CODE,
  LoomError,
  NETWORK_ERROR_CODE,
  RateLimited,
  TimeoutError,
} from "./errors.js";
import { OP_BY_NAME, type OpName } from "./generated/ops.js";
import {
  consoleLogger,
  DEFAULT_LOG_LEVEL,
  type Logger,
  type LogLevel,
  NOOP_LOGGER,
  parseLogLevel,
  redactHeaders,
  redactSecret,
  withLevel,
} from "./logging.js";
import type { TokenProvider } from "./machine.js";
import {
  backoffDelayMs,
  DEFAULT_RETRY_POLICY,
  type RetryOverride,
  type RetryPolicy,
  resolveRetryPolicy,
} from "./retry.js";
import { retryAfterFrom, retryHint } from "./retry-after.js";
import { envVar, isBrowser, runtimeIdent } from "./runtime.js";
import { decodeTokenClaims, type TokenClaims } from "./token.js";
import { SDK_IDENT } from "./version.js";

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface TransportOptions {
  baseUrl: string;
  apiKey?: string;
  /** A credential fetched per request (a bot's `MachineLogin`, a `StoredLogin`); not with `apiKey`. */
  tokenProvider?: TokenProvider;
  /** Per-call timeout, on each attempt: the wait for the token and then for the response; overridable per request. Default 60 s. */
  timeoutMs?: number;
  /** The retry policy; every field optional, validated by name. */
  retry?: RetryOverride;
  /** Where log lines go. Default: `console` at `logLevel`; pass your own sink to route them. */
  logger?: Logger;
  /** debug | info | warn | error | off. Default `LOOM_LOG_LEVEL` or `warn`. */
  logLevel?: LogLevel;
  /** Total time one call may wait on 429 Retry-After hints. Default 15 s; 0 disables. */
  ratelimitMaxWaitS?: number;
  /** The consumer label stored on every ledger row; added to each body as `consumer` unless the caller set one. */
  consumer?: string;
  /** Act on behalf of another subject (X-Loom-On-Behalf-Of); needs the impersonation grant. */
  onBehalfOf?: string;
  /** Extra headers on every request. */
  headers?: Record<string, string>;
  /** Injected fetch (tests, custom agents). Default: globalThis.fetch. */
  fetch?: FetchLike;
  /** Injected sleep (tests); it may return early on `signal`, the transport checks it after. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Injected clock for the pacing window (tests). */
  now?: () => number;
}

export interface CallOptions {
  timeoutMs?: number;
  idempotencyKey?: string;
  /** Sent as `X-Request-Id` when it is 1 to 64 letters, digits, `.`, `_` or `-` (the form the
   *  server keeps); otherwise a random id is sent, so the logs and the server name one id. */
  requestId?: string;
  signal?: AbortSignal;
  /** Per-call retry override; `{ maxAttempts: 1 }` disables retries for this call. */
  retry?: RetryOverride;
}

export interface RateState {
  limit: number;
  remaining: number;
  resetAtMs: number;
}

/** One successful call with the metadata the server sent. */
export interface CallResult<T> {
  data: T;
  /** HTTP status of the successful response. */
  status: number;
  /** The `X-Request-Id` the server echoed: finds the call in its ledger. */
  requestId: string;
  /** `X-Schema-Version`: the wire schema the server answered with. */
  schemaVersion: string;
  /** The `app` duration in `Server-Timing`: the server's own measure of the handler, if sent. */
  serverTimingMs: number | undefined;
  /** The `X-RateLimit-*` reading on this response, if the endpoint sends one. */
  rateLimit: RateState | undefined;
  /** How many HTTP attempts this call took (1 = no retry). */
  attempts: number;
  /** The store's one-line advice for this call (`X-Loom-Hint`), if sent. */
  hint?: string;
  /** The hint's code (`X-Loom-Hint-Code`), for `mentor.brief`'s adoption table. */
  hintCode?: string;
}

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

export class Transport {
  readonly baseUrl: string;
  /** Distinct `X-Loom-Hint` texts already logged by this transport. */
  private readonly hintsSeen = new Set<string>();
  /** The bearer token; a private field, invisible to JSON.stringify / inspect. */
  #apiKey: string | undefined;
  /** Hands over the bearer token per request, when there is no static key. */
  #provider: TokenProvider | undefined;
  /** Every non-credential header sent with each request. */
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;
  /** The resolved retry policy. */
  readonly retry: RetryPolicy;
  readonly log: Logger;
  private readonly ratelimitMaxWaitMs: number;
  private readonly fetchImpl: FetchLike;
  private readonly sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  private readonly now: () => number;
  private readonly rate = new Map<string, RateState>();
  /** The consumer label; the wire carries it as a body field, `consumer`. */
  readonly consumer: string;

  constructor(opts: TransportOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    // A blank or whitespace key is no key: an exported-but-empty LOOM_API_KEY
    // must not send `Bearer ` and turn a clear 401 into a puzzle.
    this.#apiKey = opts.apiKey?.trim() || undefined;
    if (this.#apiKey && opts.tokenProvider) throw new LoomError("Transport: pass apiKey or tokenProvider, not both");
    this.#provider = opts.tokenProvider;
    this.headers = {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-Loom-SDK": SDK_IDENT,
      "X-Loom-Runtime": runtimeIdent(),
      ...(opts.headers ?? {}),
    };
    // Browsers forbid setting User-Agent on fetch; everywhere else it names the SDK.
    if (!isBrowser() && !("User-Agent" in this.headers)) this.headers["User-Agent"] = SDK_IDENT;
    // `apiKey` is the one credential: a caller header of any case would ride along as a second one.
    for (const name of Object.keys(this.headers)) if (name.toLowerCase() === "authorization") delete this.headers[name];
    this.consumer = opts.consumer ?? "";
    if (opts.onBehalfOf) this.headers["X-Loom-On-Behalf-Of"] = opts.onBehalfOf;
    this.timeoutMs = opts.timeoutMs ?? 60_000;
    this.retry = resolveRetryPolicy(DEFAULT_RETRY_POLICY, opts.retry);
    this.log = resolveLogger(opts.logger, opts.logLevel);
    this.ratelimitMaxWaitMs = Math.max(0, (opts.ratelimitMaxWaitS ?? 15) * 1000);
    const f = opts.fetch ?? (globalThis.fetch as FetchLike | undefined);
    if (!f) throw new LoomError("no fetch available: pass `fetch` in TransportOptions (Node ≥ 18 has one)");
    this.fetchImpl = f;
    this.sleep = opts.sleep ?? defaultSleep;
    this.now = opts.now ?? (() => Date.now());
  }

  /** The last X-RateLimit-* reading for an op path, if the server sent one. */
  rateState(op: string): RateState | undefined {
    return this.rate.get(op);
  }

  /** Whether a key or a token provider is set; a provider may find nothing to send (no stored login, no token file), and then no Authorization header goes out. */
  get hasCredential(): boolean {
    return this.#apiKey !== undefined || this.#provider !== undefined;
  }

  /** Redacted: `…1234`, the provider's label (`bot:<client id>`), or `none`. */
  get apiKeyHint(): string {
    if (this.#provider) return this.#provider.label ?? "token provider";
    return this.#apiKey ? redactSecret(this.#apiKey) : "none";
  }

  /**
   * The bearer token's payload (org, namespaces, roles, ns_roles), decoded, not verified;
   * undefined without a JWT. A provider's token is the one in hand: none before the first request.
   */
  tokenClaims(): TokenClaims | undefined {
    return decodeTokenClaims(this.#provider ? this.#provider.current?.() : this.#apiKey);
  }

  /** What JSON.stringify and inspect show: never the credential. */
  toJSON(): Record<string, unknown> {
    return {
      baseUrl: this.baseUrl,
      apiKey: this.apiKeyHint,
      consumer: this.consumer,
      sdk: SDK_IDENT,
      retry: { ...this.retry, statuses: Array.from(this.retry.statuses) },
    };
  }

  /** POST one operation. `body` is sent as-is (the caller has typed it). */
  async post<T = unknown>(op: string, body: unknown, opts: CallOptions = {}): Promise<T> {
    return (await this.postWithMeta<T>(op, body, opts)).data;
  }

  /** `post`, returning the body with the response metadata. */
  postWithMeta<T = unknown>(op: string, body: unknown, opts: CallOptions = {}): Promise<CallResult<T>> {
    return this.send<T>("POST", op, body, opts);
  }

  /** GET `{baseUrl}/v1/{path}`: a read, so retried like a read-only op. */
  async get<T = unknown>(path: string, opts: CallOptions = {}): Promise<T> {
    return (await this.send<T>("GET", path, undefined, opts)).data;
  }

  /**
   * POST raw bytes to `{baseUrl}/v1/{path}` with `query` as its parameters
   * (`knowledge.upload`, which takes a file's bytes as the body). The server
   * keeps no Idempotency-Key for it, so it is retried only on an answer that
   * proves it never ran, and a 429 is paced as an op's is.
   */
  async postBytes<T = unknown>(
    path: string,
    query: Record<string, string>,
    data: Uint8Array<ArrayBuffer>,
    opts: CallOptions = {},
  ): Promise<T> {
    return (await this.send<T>("POST", path, data, opts, new URLSearchParams(query).toString())).data;
  }

  private async send<T>(
    method: "GET" | "POST",
    op: string,
    body: unknown,
    opts: CallOptions,
    query = "",
  ): Promise<CallResult<T>> {
    const headers: Record<string, string> = { ...this.headers };
    if (this.#apiKey) headers.Authorization = `Bearer ${this.#apiKey}`;
    const given = opts.requestId;
    const requestId = given && REQUEST_ID.test(given) ? given : randomId();
    if (given && requestId !== given) {
      this.log.warn(
        `${op} requestId ${JSON.stringify(given)} is not 1-64 letters, digits, '.', '_' or '-'; a generated id is sent`,
      );
    }
    headers["X-Request-Id"] = requestId;
    const isGet = method === "GET";
    if (isGet) delete headers["Content-Type"];
    const spec = isGet ? undefined : OP_BY_NAME[op as OpName];
    // The server keeps no replay of a read-only op: its retry just runs again.
    const idem = isGet
      ? undefined
      : (opts.idempotencyKey ?? (spec?.idempotent && !spec.readOnly ? randomId() : undefined));
    if (idem) headers["Idempotency-Key"] = idem;
    // Bytes go as they are; any other body is the op's JSON.
    const raw = body instanceof Uint8Array;
    if (raw) headers["Content-Type"] = "application/octet-stream";
    const payload = isGet
      ? undefined
      : raw
        ? (body as Uint8Array<ArrayBuffer>)
        : JSON.stringify(this.withConsumer(body));
    const url = `${this.baseUrl}/v1/${op}${query ? `?${query}` : ""}`;
    const policy = resolveRetryPolicy(this.retry, opts.retry, `call ${op}`);
    const ctx: AttemptContext = {
      op,
      method,
      url,
      payload,
      headers,
      opts,
      policy,
      idempotent: isGet || spec?.idempotent === true,
      deduplicated: spec?.idempotent === true && !spec.readOnly,
      startedAt: this.now(),
      sent: 0,
      bearer: "",
      reauthed: false,
      resending: false,
    };
    const deadline = this.now() + this.ratelimitMaxWaitMs;
    for (;;) {
      await this.paceBeforeRequest(ctx);
      try {
        return await this.postWithRetries<T>(ctx);
      } catch (e) {
        if (e instanceof RateLimited && this.ratelimitMaxWaitMs > 0) {
          const waitMs = Math.max(0, e.retryAfterS) * 1000;
          if (this.now() + waitMs <= deadline) {
            this.log.warn(`${op} 429 rate limited; waiting ${waitMs} ms (rid=${requestId})`);
            await this.pause(Math.min(waitMs, this.ratelimitMaxWaitMs), ctx);
            continue;
          }
        }
        this.log.error(`${op} failed after ${this.now() - ctx.startedAt} ms: ${describe(e)} (rid=${requestId})`);
        throw e;
      }
    }
  }

  /**
   * The body as it goes out: the Transport's `consumer` added
   * when one is set and the body names none — absent, `null`, `""` or the
   * wire's sentinel `"-"` (a generated `<Op>Input` may carry the field
   * empty). A body that names its own consumer wins; with no Transport
   * consumer the body is sent exactly as given.
   */
  private withConsumer(body: unknown): unknown {
    if (!this.consumer || !body || typeof body !== "object" || Array.isArray(body)) return body ?? {};
    const b = body as Record<string, unknown>;
    const own = b.consumer;
    const named = typeof own === "string" ? own.trim() !== "" && own.trim() !== "-" : own != null;
    return named ? b : { ...b, consumer: this.consumer };
  }

  private async paceBeforeRequest(ctx: AttemptContext): Promise<void> {
    const st = this.rate.get(ctx.op);
    if (!st || st.remaining > 0 || st.limit <= 0) return;
    const untilReset = st.resetAtMs - this.now();
    if (untilReset > 0) await this.pause(Math.min(untilReset, this.ratelimitMaxWaitMs || untilReset), ctx);
  }

  /** Sleep, cut short by the caller's signal, which then rejects the call. */
  private async pause(ms: number, ctx: AttemptContext): Promise<void> {
    await this.sleep(ms, ctx.opts.signal);
    throwIfAborted(ctx);
  }

  /** One request and its body, under the per-call timeout and the caller's signal. */
  private async exchange(ctx: AttemptContext, timeoutMs: number): Promise<Exchange> {
    const signal = ctx.opts.signal;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const onOuter = () => ctrl.abort();
    signal?.addEventListener("abort", onOuter, { once: true });
    try {
      let res: Response;
      try {
        res = await this.fetchImpl(ctx.url, {
          method: ctx.method,
          headers: ctx.headers,
          body: ctx.payload,
          signal: ctrl.signal,
        });
      } catch (e) {
        return { failed: "connect", error: e };
      }
      try {
        return { res, text: await readBody(res, ctrl.signal) };
      } catch (e) {
        return { failed: "body", error: e };
      }
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onOuter);
    }
  }

  /** The provider's token on this attempt's headers; after a 401, a fresh one. */
  private async authorize(ctx: AttemptContext, timeoutMs: number): Promise<void> {
    const provider = this.#provider;
    if (!provider) return;
    if (provider.origin && new URL(ctx.url).origin !== provider.origin)
      throw new AuthError(`refusing to send ${provider.label ?? "the provider"}'s token to ${new URL(ctx.url).host}`, {
        op: ctx.op,
      });
    throwIfAborted(ctx);
    // The caller may give up on its wait, or time out; a renewal other calls share goes on.
    const refused = ctx.refused;
    ctx.bearer = await untilTimeoutOrAbort(provider.token(refused), timeoutMs, ctx);
    ctx.refused = undefined;
    // No fresh token to send (a token file, a key that cannot renew): the 401 stands.
    if (refused !== undefined && ctx.bearer === refused && ctx.refusal) throw ctx.refusal;
    // A provider with nothing to send (no stored login, no token file) sends no header.
    if (ctx.bearer) ctx.headers.Authorization = `Bearer ${ctx.bearer}`;
    else delete ctx.headers.Authorization;
  }

  private recordRate(op: string, r: Response): void {
    const limit = Number(r.headers.get("X-RateLimit-Limit") ?? "");
    const remaining = Number(r.headers.get("X-RateLimit-Remaining") ?? "");
    const reset = Number(r.headers.get("X-RateLimit-Reset") ?? "");
    if (
      Number.isFinite(limit) &&
      Number.isFinite(remaining) &&
      Number.isFinite(reset) &&
      r.headers.has("X-RateLimit-Limit")
    ) {
      this.rate.set(op, { limit, remaining, resetAtMs: this.now() + reset * 1000 });
    }
  }

  private async postWithRetries<T>(ctx: AttemptContext): Promise<CallResult<T>> {
    const { op, method, url, payload, headers, opts, policy } = ctx;
    const timeoutMs = opts.timeoutMs ?? this.timeoutMs;
    const requestId = headers["X-Request-Id"] ?? "";
    let sleptMs = 0;
    const budgetLeft = () => policy.totalBackoffBudgetMs - sleptMs;
    /** Sleep before the next attempt, or return false when the policy says stop. */
    const retryAfter = async (attempt: number, why: string, hintS?: number): Promise<boolean> => {
      if (attempt >= policy.maxAttempts) return false;
      let delay = backoffDelayMs(policy, attempt);
      if (policy.respectRetryAfter && hintS !== undefined && hintS <= policy.maxRetryAfterS)
        delay = Math.round(hintS * 1000);
      if (delay > budgetLeft()) return false;
      this.log.warn(
        `${op} attempt ${attempt}/${policy.maxAttempts} ${why}; retrying in ${delay} ms (rid=${requestId})`,
      );
      await this.pause(delay, ctx);
      sleptMs += delay;
      return true;
    };
    for (let attempt = 1; ; attempt++) {
      // A resend with a fresh token is the same attempt, as the Python SDK counts it.
      if (!ctx.resending) ctx.sent++;
      ctx.resending = false;
      headers["X-Loom-Retry-Count"] = String(ctx.sent - 1);
      const asked = Date.now();
      if (this.#provider) {
        try {
          await this.authorize(ctx, timeoutMs);
        } catch (e) {
          // The token endpoint was away: the op was never sent, so it is retried
          // as a connection failure is, under the same send count.
          if (
            e instanceof BackendUnavailable &&
            policy.retryConnectionErrors &&
            (await retryAfter(attempt, `token unavailable: ${describe(e)}`))
          ) {
            ctx.resending = true;
            continue;
          }
          throw e;
        }
      }
      // What the token wait left of this attempt's timeout.
      const responseMs = Math.max(0, timeoutMs - (Date.now() - asked));
      this.log.debug(
        `${op} → ${method} ${url} attempt ${attempt} waits ${responseMs} of ${timeoutMs} ms bytes ${payload?.length ?? 0}`,
        redactHeaders(headers),
      );
      throwIfAborted(ctx);
      const t0 = this.now();
      const ex = await this.exchange(ctx, responseMs);
      if ("failed" in ex) {
        const e = ex.error;
        if (isAbort(e)) {
          throwIfAborted(ctx, e);
          const err = new TimeoutError(`${op}: no response within the call's ${timeoutMs} ms timeout`, {
            op,
            cause: e,
            requestId,
          });
          const may = policy.retryTimeouts === "always" || (policy.retryTimeouts === "idempotent" && ctx.deduplicated);
          if (may && (await retryAfter(attempt, `ran past the call's ${timeoutMs} ms timeout`))) continue;
          throw err;
        }
        if (ex.failed === "body") {
          // The server answered, so it may have acted: not retried.
          throw new BackendUnavailable(`${op}: response from ${this.baseUrl} cut off: ${describe(e)}`, {
            op,
            cause: e,
            requestId,
            code: NETWORK_ERROR_CODE,
          });
        }
        // connect failure: the server never saw it. Name the
        // server the SDK could not reach and give the error a stable code, so
        // a wrong base URL reads as one thing and not `status 0 code ""`.
        if (policy.retryConnectionErrors && (await retryAfter(attempt, `connection failed: ${describe(e)}`))) continue;
        throw new BackendUnavailable(`${op}: cannot reach ${this.baseUrl}: ${describe(e)}`, {
          op,
          cause: e,
          requestId,
          code: NETWORK_ERROR_CODE,
        });
      }
      const { res, text } = ex;
      this.recordRate(op, res);
      if (res.status === 401 && this.#provider && ctx.bearer && !ctx.reauthed) {
        // The token may have been revoked or rotated: once more with a fresh
        // one. The server refused before dispatch, and this is no policy retry.
        ctx.reauthed = true;
        ctx.resending = true;
        ctx.refused = ctx.bearer;
        ctx.refusal = errorFromResponse(op, res.status, parseBody(text), res.headers.get("X-Request-Id") ?? requestId);
        this.log.info(`${op} 401; retrying once with a fresh token (rid=${requestId})`);
        attempt--;
        continue;
      }
      const echoed = res.headers.get("X-Request-Id") ?? requestId;
      const elapsed = this.now() - t0;
      if (res.ok) {
        let data: T;
        try {
          data = (text ? JSON.parse(text) : {}) as T;
        } catch (e) {
          throw new LoomError(`${op}: HTTP ${res.status} response is not JSON (${text.length} bytes)`, {
            op,
            status: res.status,
            code: INVALID_RESPONSE_CODE,
            requestId: echoed,
            detail: { message: text.slice(0, 200) },
            cause: e,
          });
        }
        this.log.info(`${op} ${res.status} ${elapsed} ms attempts=${ctx.sent} rid=${echoed}`);
        const hint = res.headers.get("X-Loom-Hint") ?? undefined;
        const hintCode = res.headers.get("X-Loom-Hint-Code") ?? undefined;
        if (hint && !this.hintsSeen.has(hint)) {
          // The store mentors the agent; a bot with no pack to
          // render still sees the advice in its own log, once per distinct text.
          this.hintsSeen.add(hint);
          this.log.info(`loom hint (${hintCode ?? ""}) on ${op}: ${hint}`);
        }
        return {
          data,
          status: res.status,
          requestId: echoed,
          schemaVersion: res.headers.get("X-Schema-Version") ?? "",
          serverTimingMs: appDurationMs(res.headers.get("Server-Timing")),
          rateLimit: this.rate.get(op),
          attempts: ctx.sent,
          hint,
          hintCode,
        };
      }
      const body = parseBody(text);
      const retryAfterS = res.status === 429 ? retryAfterFrom(res.headers, this.now()) : undefined;
      const err = errorFromResponse(op, res.status, body, echoed, retryAfterS);
      if (err instanceof RateLimited) throw err;
      const retried =
        policy.statuses.has(res.status) &&
        !CONFIGURATION_CODES.has(err.code) &&
        (ctx.idempotent || neverRan(res.status, err.code));
      if (retried) {
        const hintS = retryHint(res.headers, err.detail, this.now());
        if (await retryAfter(attempt, `${res.status}${err.code ? ` ${err.code}` : ""}`, hintS)) continue;
        if (attempt < policy.maxAttempts)
          this.log.warn(`${op}: backoff budget of ${policy.totalBackoffBudgetMs} ms spent; giving up`);
      }
      throw err;
    }
  }
}

/**
 * A retried status re-runs the op: only an idempotent op's Idempotency-Key is
 * deduplicated, so any other op is retried only on an answer that proves the
 * handler never ran — a 408 (the request never fully arrived) or a refusal
 * the server makes before dispatch.
 */
const NEVER_RAN_CODES: ReadonlySet<string> = new Set(["AUTH_UNAVAILABLE"]);

/**
 * 5xx answers to a deployment's configuration, which no retry changes: the
 * operator's fix is named in the error (and in /v1/health). A model call that
 * names no customer is a server path to fix, not a passing fault.
 */
const CONFIGURATION_CODES: ReadonlySet<string> = new Set([
  "CODED_WIDTH_MISMATCH",
  "GATEWAY_REQUIRED",
  "MODEL_CALL_UNATTRIBUTED",
]);

function neverRan(status: number, code: string): boolean {
  return status === 408 || NEVER_RAN_CODES.has(code);
}

interface AttemptContext {
  op: string;
  method: "GET" | "POST";
  url: string;
  /** The JSON body, or a file's bytes; none on a GET. */
  payload: string | Uint8Array<ArrayBuffer> | undefined;
  headers: Record<string, string>;
  opts: CallOptions;
  policy: RetryPolicy;
  idempotent: boolean;
  /** The server dedups its Idempotency-Key (idempotent, not read-only): a timeout may be retried. */
  deduplicated: boolean;
  startedAt: number;
  /** HTTP attempts sent so far, across every 429 pacing round. */
  sent: number;
  /** The provider's token on the last attempt. */
  bearer: string;
  /** A 401 has been answered with a fresh token; not again. */
  reauthed: boolean;
  /** The next send repeats this attempt with a fresh token. */
  resending: boolean;
  /** The token the server just refused, which the next attempt replaces. */
  refused?: string;
  /** The error for that 401, raised when the provider has no other token. */
  refusal?: LoomError;
}

/** An error answer's JSON, else its text. */
function parseBody(text: string): unknown {
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return text;
  }
}

function resolveLogger(sink: Logger | undefined, level: LogLevel | undefined): Logger {
  const lvl = level ?? levelFromEnv(sink ?? consoleLogger);
  if (lvl === "off") return NOOP_LOGGER;
  return withLevel(sink ?? consoleLogger, lvl);
}

/** `LOOM_LOG_LEVEL`, or the default: an unreadable value is a warning, not a failed client. */
function levelFromEnv(sink: Logger): LogLevel {
  const fromEnv = envVar("LOOM_LOG_LEVEL")?.trim();
  if (!fromEnv) return DEFAULT_LOG_LEVEL;
  try {
    return parseLogLevel(fromEnv, "LOOM_LOG_LEVEL");
  } catch (e) {
    sink.warn(`${e instanceof Error ? e.message : String(e)}; using ${DEFAULT_LOG_LEVEL}`);
    return DEFAULT_LOG_LEVEL;
  }
}

type Exchange = { res: Response; text: string } | { failed: "connect" | "body"; error: unknown };

/** The body text, abandoned when `signal` fires even if an injected fetch did not wire it to the stream. */
function readBody(res: Response, signal: AbortSignal): Promise<string> {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<string>((resolve, reject) => {
    const onAbort = () => {
      res.body?.cancel().catch(() => undefined);
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    res.text().then(
      (text) => {
        signal.removeEventListener("abort", onAbort);
        resolve(text);
      },
      (e: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(e);
      },
    );
  });
}

function abortError(): Error {
  return Object.assign(new Error("aborted"), { name: "AbortError" });
}

/**
 * `work`, or a TimeoutError once `ms` pass, or the caller's abort, whichever
 * comes first. Either way the timer and the abort listener go, so a cancelled
 * call holds nothing open; `work` itself goes on for whoever else waits on it.
 */
function untilTimeoutOrAbort<T>(work: T | Promise<T>, ms: number, ctx: AttemptContext): Promise<T> {
  const signal = ctx.opts.signal;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  const cut = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      const requestId = ctx.headers["X-Request-Id"] ?? "";
      reject(new TimeoutError(`${ctx.op}: no token within ${ms} ms`, { op: ctx.op, requestId }));
    }, ms);
    onAbort = () => {
      try {
        throwIfAborted(ctx);
      } catch (e) {
        reject(e);
      }
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
  return Promise.race([Promise.resolve(work), cut]).finally(() => {
    clearTimeout(timer);
    if (onAbort) signal?.removeEventListener("abort", onAbort);
  });
}

/** Throws once the caller's signal has fired. */
function throwIfAborted(ctx: AttemptContext, cause?: unknown): void {
  if (!ctx.opts.signal?.aborted) return;
  const requestId = ctx.headers["X-Request-Id"] ?? "";
  throw new LoomError(`${ctx.op}: aborted by caller`, {
    op: ctx.op,
    cause: cause ?? ctx.opts.signal.reason,
    requestId,
  });
}

function describe(e: unknown): string {
  if (e instanceof LoomError) return `${e.name}${e.code ? ` ${e.code}` : ""}${e.status ? ` (${e.status})` : ""}`;
  if (e instanceof Error) return e.message;
  return String(e);
}

function isAbort(e: unknown): boolean {
  return !!e && typeof e === "object" && (e as { name?: string }).name === "AbortError";
}

// The request ids the server keeps as sent; it replaces any other with its own.
const REQUEST_ID = /^[A-Za-z0-9._-]{1,64}$/;

export function randomId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

/** The `dur` of the `app` metric in a `Server-Timing` header, or undefined when absent. */
export function appDurationMs(serverTiming: string | null): number | undefined {
  for (const metric of (serverTiming ?? "").split(",")) {
    const [name, ...params] = metric.split(";").map((p) => p.trim());
    if (name !== "app") continue;
    for (const param of params) {
      const [key, value] = param.split("=");
      if (key?.trim() === "dur") {
        const n = Number(value);
        return Number.isFinite(n) ? n : undefined;
      }
    }
  }
  return undefined;
}
