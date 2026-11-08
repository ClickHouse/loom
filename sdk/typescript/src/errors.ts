import { DEFAULT_RETRY_AFTER_S } from "./retry-after.js";

/**
 * Typed errors for the Loom wire.
 *
 * Every failed call raises a `LoomError` carrying the HTTP status, the server's
 * `detail.code`, its message, the request id echoed by the server and the raw
 * `detail` object. Subclasses mirror the Python SDK's hierarchy so a caller can
 * `catch (e) { if (e instanceof NotFound) … }` in either language.
 */

export interface LoomErrorDetail {
  code?: string;
  message?: string;
  [key: string]: unknown;
}

const CATEGORIES = ["input", "auth", "capacity", "dependency", "configuration", "internal"] as const;

/** Whose move an error is (`LoomError.category`). */
export type ErrorCategory = (typeof CATEGORIES)[number];

export interface LoomErrorOptions {
  status?: number;
  code?: string;
  requestId?: string;
  detail?: LoomErrorDetail;
  op?: string;
  cause?: unknown;
}

// The SDK's own `[category, retryable]` for an error whose body names none: kept off the
// error itself, so it is no part of the public API.
const SDK_CLASS = new WeakMap<LoomError, readonly [ErrorCategory, boolean]>();

/** `err`, with the SDK's own class for when the server's `detail` names none. */
export function classed<E extends LoomError>(err: E, category: ErrorCategory, retryable: boolean): E {
  SDK_CLASS.set(err, [category, retryable]);
  return err;
}

/** A sign-in answer that can pass later (a 429 or a 5xx), classed retryable. */
export function busy(message: string, opts: { status: number; code?: string }): BackendUnavailable {
  return classed(new BackendUnavailable(message, opts), opts.status === 429 ? "capacity" : "dependency", true);
}

export class LoomError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  readonly detail: LoomErrorDetail;
  readonly op: string;

  constructor(message: string, opts: LoomErrorOptions = {}) {
    super(message, opts.cause !== undefined ? { cause: opts.cause } : undefined);
    this.name = new.target.name;
    this.status = opts.status ?? 0;
    this.code = opts.code ?? "";
    this.requestId = opts.requestId ?? "";
    this.detail = opts.detail ?? {};
    this.op = opts.op ?? "";
  }

  /**
   * Whose move it is: the server's `detail.category` (`input`, `auth`, `capacity`,
   * `dependency`, `configuration` or `internal`), else the SDK's own class (the Errors
   * section of the TypeScript SDK reference).
   */
  get category(): ErrorCategory {
    const named = this.detail.category;
    return typeof named === "string" && (CATEGORIES as readonly string[]).includes(named)
      ? (named as ErrorCategory)
      : fallbackClass(this)[0];
  }

  /**
   * The server's `detail.retryable`: resending the same request unchanged can succeed
   * later. Else the SDK's own class, as for `category`.
   */
  get retryable(): boolean {
    const named = this.detail.retryable;
    return typeof named === "boolean" ? named : fallbackClass(this)[1];
  }
}

/**
 * 5xx codes never resent, with their category: a deployment's configuration (the
 * operator's fix is named in the error and in /v1/health), a model call that names no
 * customer (a server path to fix, not a passing fault), and a write already committed
 * whose rows are still on their way.
 */
export const NEVER_RETRIED: ReadonlyMap<string, ErrorCategory> = new Map<string, ErrorCategory>([
  ["ANALYSIS_UNAVAILABLE", "configuration"],
  ["ASSISTANT_UNAVAILABLE", "configuration"],
  ["CODED_WIDTH_MISMATCH", "configuration"],
  ["GATEWAY_REQUIRED", "configuration"],
  ["MODEL_CALL_UNATTRIBUTED", "internal"],
  ["PUBLICATION_PENDING", "dependency"],
]);

/**
 * The class of an error whose body names none: one the SDK raised itself (no response
 * or none in time, an unreadable answer, a call it could not send), a code the SDK never
 * retries, else the server's rule by HTTP status (a proxy's page, or a server without the
 * fields).
 */
function fallbackClass(e: LoomError): readonly [ErrorCategory, boolean] {
  const own = SDK_CLASS.get(e);
  if (own) return own;
  const { code, status } = e;
  // A call with no response may have run.
  if (code === NETWORK_ERROR_CODE) return ["dependency", false];
  if (code === INVALID_RESPONSE_CODE) return ["internal", false];
  const never = NEVER_RETRIED.get(code);
  if (never) return [never, false];
  if (status === 0) {
    // Raised before the call was sent: a service the SDK needed first (an identity
    // provider, a secret store) is down, the caller is not signed in, or the call itself
    // is wrong.
    if (e instanceof BackendUnavailable) return ["dependency", true];
    if (e instanceof AuthError) return ["auth", false];
    return ["input", false];
  }
  if (status === 401 || status === 403) return ["auth", false];
  if (status === 408 || status === 429) return ["capacity", true];
  if (status === 502 || status === 503 || status === 504) return ["dependency", true];
  if (status >= 500) return ["internal", false];
  return ["input", false];
}

/** 400 / 422: the request body did not validate. */
export class ValidationError extends LoomError {}
/** 401 / 403: no or insufficient authorization for the op / namespace. */
export class AuthError extends LoomError {}
/** 404: the memory, document or namespace is not there. */
export class NotFound extends LoomError {}
/** 409 / 412: a version, idempotency or lifecycle precondition failed. */
export class PreconditionFailed extends LoomError {}
/** 409 with code IDEMPOTENT_CONFLICT: the Idempotency-Key was reused with a different body. */
export class IdempotentConflict extends PreconditionFailed {}
/** 429: rate limited past the pacing budget; `retryAfterS` is the server's hint. */
export class RateLimited extends LoomError {
  readonly retryAfterS: number;
  constructor(message: string, retryAfterS: number, opts: LoomErrorOptions = {}) {
    super(message, opts);
    this.retryAfterS = retryAfterS;
  }
}
/** 5xx after the transport's retries, or a connection that never got a response. */
export class BackendUnavailable extends LoomError {}
/**
 * 503 with code IDEMPOTENT_IN_PROGRESS: the first request with this
 * Idempotency-Key still runs. Retried after `retryAfterS` (the transport does,
 * for an idempotent op); the retry gets the first request's answer.
 */
export class IdempotentInProgress extends BackendUnavailable {
  readonly retryAfterS: number;
  constructor(message: string, retryAfterS: number, opts: LoomErrorOptions = {}) {
    super(message, opts);
    this.retryAfterS = retryAfterS;
  }
}
/**
 * `code` of a `BackendUnavailable` whose request got no complete response. Its message
 * names the base URL: `<op>: cannot reach <baseUrl>: …` (the connection failed),
 * `no response from <baseUrl>: …` or `response from <baseUrl> cut off: …` (the call may
 * have run); `.retryable` says which. Stable, so a caller can branch on it the way it
 * branches on a server `detail.code`.
 */
export const NETWORK_ERROR_CODE = "NETWORK";
/**
 * `code` of a `LoomError` for a 2xx whose body is not JSON — a proxy or
 * gateway page, most often. The request may have been processed, so it is
 * not retried; `detail.message` holds the first 200 characters of the body.
 */
export const INVALID_RESPONSE_CODE = "INVALID_RESPONSE";
/** The request exceeded the per-call timeout. */
export class TimeoutError extends LoomError {}

/** Map a non-2xx response to the error subclass the Python SDK would raise; `retryAfterS` feeds a 429's `RateLimited`. */
export function errorFromResponse(
  op: string,
  status: number,
  body: unknown,
  requestId: string,
  retryAfterS = DEFAULT_RETRY_AFTER_S,
): LoomError {
  const detail = extractDetail(body);
  const code = String(detail.code ?? "");
  const message = String(detail.message ?? "") || `${op}: HTTP ${status}`;
  const opts: LoomErrorOptions = { status, code, requestId, detail, op };
  if (status === 429) return new RateLimited(message, retryAfterS, opts);
  if (status === 400 || status === 422) return new ValidationError(message, opts);
  if (status === 401 || status === 403) return new AuthError(message, opts);
  if (status === 404) return new NotFound(message, opts);
  if (status === 409 && code === "IDEMPOTENT_CONFLICT") return new IdempotentConflict(message, opts);
  // ClickHouse refused to store an override: no precondition, and not retried.
  if (status === 409 && code === "OVERRIDE_NOT_STORED") return new LoomError(message, opts);
  if (status === 409 || status === 412) return new PreconditionFailed(message, opts);
  if (status === 503 && code === "IDEMPOTENT_IN_PROGRESS") return new IdempotentInProgress(message, retryAfterS, opts);
  if (status >= 500) return new BackendUnavailable(message, opts);
  return new LoomError(message, opts);
}

function extractDetail(body: unknown): LoomErrorDetail {
  if (body && typeof body === "object") {
    const d = (body as { detail?: unknown }).detail;
    if (d && typeof d === "object") return d as LoomErrorDetail;
    if (typeof d === "string") return { message: d };
    return body as LoomErrorDetail;
  }
  if (typeof body === "string" && body) return { message: body.slice(0, 500) };
  return {};
}

// Node's fetch failures that come before any byte of the request is sent.
const CONNECT_CODES: ReadonlySet<string> = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "UND_ERR_CONNECT_TIMEOUT",
]);

interface ErrorLike {
  code?: unknown;
  syscall?: unknown;
  cause?: unknown;
  errors?: unknown;
}

// How deep connectFailed follows `cause` and `errors` before giving up.
const CAUSE_DEPTH = 4;

/**
 * Whether `e`, or an error it wraps, is a failure to connect: the request never left, so
 * resending it is safe. Reads `code` on the error, its causes and each entry of an
 * `AggregateError` (Node's fetch sets it on the cause, node-fetch on the error); a timeout
 * counts when it timed out connecting. Other runtimes' fetch errors name no such code.
 */
export function connectFailed(e: unknown): boolean {
  const seen = (x: unknown, depth: number): boolean => {
    if (!x || typeof x !== "object" || depth > CAUSE_DEPTH) return false;
    const err = x as ErrorLike;
    if (typeof err.code === "string" && CONNECT_CODES.has(err.code)) return true;
    if (err.code === "ETIMEDOUT" && err.syscall === "connect") return true;
    if (Array.isArray(err.errors) && err.errors.some((y) => seen(y, depth + 1))) return true;
    return seen(err.cause, depth + 1);
  };
  return seen(e, 0);
}
