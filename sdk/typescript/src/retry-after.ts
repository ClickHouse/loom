/**
 * `Retry-After` parsing. The header is either a number of
 * seconds (`"2"`, `"0.5"`) or an HTTP-date (RFC 7231, `Wed, 21 Oct 2026
 * 07:28:00 GMT`). A bare `Number(header)` is NaN on a date and would
 * silently fall back to one second.
 */

/** The longest hint the SDK believes; a longer or unparseable one becomes `DEFAULT_RETRY_AFTER_S`. */
export const MAX_RETRY_AFTER_S = 60;
export const DEFAULT_RETRY_AFTER_S = 1;

const ASCTIME = /^[A-Za-z]{3} [A-Za-z]{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}$/;

/**
 * Seconds to wait, measured from `nowMs` (default `Date.now()`), or
 * `undefined` when the header is absent or unparseable. A date in the past
 * is 0; a negative number is 0.
 */
export function parseRetryAfter(header: string | null | undefined, nowMs?: number): number | undefined {
  if (header == null) return undefined;
  const raw = header.trim();
  if (!raw) return undefined;
  if (/^[+-]?\d+(\.\d+)?$/.test(raw)) return Math.max(0, Number(raw));
  // The asctime form names no zone and Date.parse would read it as local time; HTTP-dates are UTC.
  const at = Date.parse(ASCTIME.test(raw) ? `${raw} GMT` : raw);
  if (!Number.isFinite(at)) return undefined;
  const base = nowMs ?? Date.now();
  return Math.max(0, (at - base) / 1000);
}

/** The wait a response asks for: its `Retry-After`, else the error body's `detail.retry_after_s` (a server that sends only the body field). */
export function retryHint(headers: Headers, detail: Record<string, unknown>, nowMs?: number): number | undefined {
  const raw = headers.get("Retry-After") ?? (detail.retry_after_s == null ? null : String(detail.retry_after_s));
  return parseRetryAfter(raw, nowMs);
}

/** The bounded wait a 429 / 503 response asks for. */
export function retryAfterFrom(headers: Headers, nowMs?: number): number {
  const parsed = parseRetryAfter(headers.get("Retry-After"), nowMs);
  if (parsed === undefined || parsed > MAX_RETRY_AFTER_S) return DEFAULT_RETRY_AFTER_S;
  return parsed;
}
