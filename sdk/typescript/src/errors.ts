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

export interface LoomErrorOptions {
  status?: number;
  code?: string;
  requestId?: string;
  detail?: LoomErrorDetail;
  op?: string;
  cause?: unknown;
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
 * `code` of a `BackendUnavailable` whose request never got a response — the
 * connection failed (wrong host or port, DNS, refused). Its message names the
 * base URL: `<op>: cannot reach <baseUrl>: <reason>`. Stable,
 * so a caller can branch on it the way it branches on a server `detail.code`.
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
