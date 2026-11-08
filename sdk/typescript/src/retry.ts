/**
 * The retry policy: one object, validated field by field,
 * set on the client and overridable per call.
 *
 * What is retried and why:
 * - connection failures (the server never saw the request);
 * - HTTP statuses in `statuses` — 408 and 5xx by default — for an op the
 *   registry marks idempotent (its `Idempotency-Key` is deduplicated); any
 *   other op only on a 408 or a refusal made before dispatch (503
 *   AUTH_UNAVAILABLE), since a 5xx after the handler started may have written;
 * - a per-call timeout only when the op is idempotent by the registry and
 *   not read-only (`retryTimeouts: "idempotent"`), because the request
 *   carried an `Idempotency-Key` the server dedups on; any other op that
 *   timed out may still be executing (a read-only `memory.ask` would run its
 *   synthesis twice) and is never retried by default;
 * - a response cut off or a socket dropped after sending, by the same rule as
 *   a timeout: the call may have run, as a timed-out one may.
 * A 429 is not a retry: it is pacing, bounded by
 * `ratelimitMaxWaitS`.
 */

import { LoomError } from "./errors.js";
import { MAX_RETRY_AFTER_S } from "./retry-after.js";

export type RetryTimeouts = "never" | "idempotent" | "always";

export interface RetryPolicy {
  /** Total attempts including the first. 1 disables retries. */
  maxAttempts: number;
  /** First backoff; doubles per attempt. */
  backoffMs: number;
  /** Ceiling on one backoff sleep. */
  backoffMaxMs: number;
  /** 0..1: each sleep is `delay × (1 − jitter/2 + random × jitter)`. */
  backoffJitter: number;
  /** HTTP statuses retried (a 429 is paced, not retried). */
  statuses: ReadonlySet<number>;
  /** Retry when the connection failed (Node names a connect-phase error). */
  retryConnectionErrors: boolean;
  /** Retry a per-call timeout, a cut-off response or a socket dropped after sending: never,
   *  only for ops the server dedups (registry-idempotent, not read-only), or always. */
  retryTimeouts: RetryTimeouts;
  /** Honour a `Retry-After` on a retried status instead of the backoff. */
  respectRetryAfter: boolean;
  /** Longest `Retry-After` honoured; beyond it the backoff applies. */
  maxRetryAfterS: number;
  /** Total time one call may spend sleeping between attempts; `Infinity` = unbounded. */
  totalBackoffBudgetMs: number;
}

const range = (from: number, to: number): number[] => Array.from({ length: to - from }, (_, i) => from + i);

export const DEFAULT_RETRY_POLICY: RetryPolicy = Object.freeze({
  maxAttempts: 3,
  backoffMs: 250,
  backoffMaxMs: 5_000,
  backoffJitter: 0.5,
  statuses: new Set([408, ...range(500, 600)]),
  retryConnectionErrors: true,
  retryTimeouts: "idempotent",
  respectRetryAfter: true,
  maxRetryAfterS: MAX_RETRY_AFTER_S,
  totalBackoffBudgetMs: Number.POSITIVE_INFINITY,
});

export type RetryOverride = Partial<Omit<RetryPolicy, "statuses">> & { statuses?: Iterable<number> };

function nonNegative(name: string, v: number, source: string): number {
  if (typeof v !== "number" || Number.isNaN(v) || v < 0) {
    throw new LoomError(`${source}: retry.${name} must be a non-negative number, got ${String(v)}`);
  }
  return v;
}

/** Merge an override onto a base policy, validating every field it sets and naming the offender. */
export function resolveRetryPolicy(base: RetryPolicy, override?: RetryOverride, source = "LoomClient"): RetryPolicy {
  if (!override) return base;
  const p: RetryPolicy = { ...base, statuses: override.statuses ? new Set(override.statuses) : base.statuses };
  if (override.maxAttempts !== undefined) {
    const n = override.maxAttempts;
    if (!Number.isInteger(n) || n < 1)
      throw new LoomError(`${source}: retry.maxAttempts must be an integer ≥ 1, got ${String(n)}`);
    p.maxAttempts = n;
  }
  if (override.backoffMs !== undefined) p.backoffMs = nonNegative("backoffMs", override.backoffMs, source);
  if (override.backoffMaxMs !== undefined) p.backoffMaxMs = nonNegative("backoffMaxMs", override.backoffMaxMs, source);
  if (override.backoffJitter !== undefined) {
    const j = nonNegative("backoffJitter", override.backoffJitter, source);
    if (j > 1) throw new LoomError(`${source}: retry.backoffJitter must be within 0..1, got ${j}`);
    p.backoffJitter = j;
  }
  if (override.maxRetryAfterS !== undefined)
    p.maxRetryAfterS = nonNegative("maxRetryAfterS", override.maxRetryAfterS, source);
  if (override.totalBackoffBudgetMs !== undefined) {
    p.totalBackoffBudgetMs = nonNegative("totalBackoffBudgetMs", override.totalBackoffBudgetMs, source);
  }
  if (override.retryTimeouts !== undefined) {
    if (!["never", "idempotent", "always"].includes(override.retryTimeouts)) {
      throw new LoomError(
        `${source}: retry.retryTimeouts must be never | idempotent | always, got ${String(override.retryTimeouts)}`,
      );
    }
    p.retryTimeouts = override.retryTimeouts;
  }
  if (override.retryConnectionErrors !== undefined) p.retryConnectionErrors = override.retryConnectionErrors;
  if (override.respectRetryAfter !== undefined) p.respectRetryAfter = override.respectRetryAfter;
  for (const s of p.statuses) {
    if (!Number.isInteger(s) || s < 100 || s > 599)
      throw new LoomError(`${source}: retry.statuses holds a non-HTTP status ${String(s)}`);
  }
  return p;
}

/** The backoff before attempt `attempt + 1` (attempt is 1-based, the one that just failed). */
export function backoffDelayMs(policy: RetryPolicy, attempt: number, random: () => number = Math.random): number {
  const exponential = Math.min(policy.backoffMs * 2 ** (attempt - 1), policy.backoffMaxMs);
  const j = policy.backoffJitter;
  return Math.round(exponential * (1 - j / 2 + random() * j));
}
