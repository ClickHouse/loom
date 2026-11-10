/**
 * The typed error a failed Loom request throws.
 *
 * The server answers every failure with `{"detail": {"code", "message", …}}`
 * (`server/core/errors.py::LoomServerError.to_detail`, the `HTTPException`s in
 * `server/main.py` / `server/insights/app/admin_routes.py` / `server/auth/login.py`) and
 * stamps the request id on the `X-Request-Id` response header
 * (`server/transport/http.py`). FastAPI's own 422 uses `detail: [{loc, msg,
 * type}]`, and a reverse proxy in front of Loom may answer with plain text or
 * HTML. `apiErrorFromResponse` reads all four shapes into one object so the
 * UI never has to show a status code followed by a JSON blob again.
 */
import { isRecord } from "./guards";

export class ApiError extends Error {
  readonly status: number;
  /** Server error code (`AUTH_INSUFFICIENT`, `RATE_LIMITED`, …); "" when the
   *  body carried none. */
  readonly code: string;
  /** `X-Request-Id` of the failed response; "" when the header was absent. */
  readonly requestId: string;
  /** The server's own message, "" when the body carried none. `message`
   *  (the Error field) falls back to `HTTP <status>` so a bare `String(err)`
   *  still says something. */
  readonly serverMessage: string;
  /** The parsed `detail` object, for callers that need an extra field
   *  (`current_sha256`, `retry_after_s`, …). */
  readonly detail: Record<string, unknown>;
  /** Seconds from the `Retry-After` header, 0 when absent: some refusals (an auth store
   *  that did not answer, a request with this key still running) send no body field. */
  readonly retryAfter: number;
  /** The server refused the stored token on this call, which ended the session (`api.ts`
   *  request): whatever the refusal's words, it reads as an ended session. */
  sessionEnded = false;

  constructor(opts: {
    status: number;
    code?: string;
    message?: string;
    requestId?: string;
    detail?: Record<string, unknown>;
    retryAfter?: number;
  }) {
    super(opts.message || `HTTP ${opts.status}`);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code ?? "";
    this.requestId = opts.requestId ?? "";
    this.serverMessage = opts.message ?? "";
    this.detail = opts.detail ?? {};
    this.retryAfter = opts.retryAfter ?? 0;
  }
}

export interface ParsedErrorBody {
  code: string;
  message: string;
  detail: Record<string, unknown>;
}

/** Read `{detail: …}` (object, string or FastAPI's list form) or a plain-text
 *  body into code + message. Never returns the raw JSON as the message: a
 *  body that is JSON but carries no `detail` yields an empty message, and a
 *  body that starts with `<` (a proxy's HTML page) is dropped too. */
export function parseErrorBody(text: string): ParsedErrorBody {
  const out: ParsedErrorBody = { code: "", message: "", detail: {} };
  const trimmed = (text || "").trim();
  if (!trimmed) return out;
  let body: unknown;
  try {
    body = JSON.parse(trimmed);
  } catch {
    // Plain text from a proxy or a crashed worker. Keep a short line of it
    // unless it is markup, which nobody should read in a toast.
    if (!trimmed.startsWith("<")) out.message = trimmed.slice(0, 200);
    return out;
  }
  if (!isRecord(body)) return out;
  const detail = body.detail;
  if (typeof detail === "string") {
    out.message = detail;
  } else if (Array.isArray(detail)) {
    // FastAPI request validation: one line per failing field.
    out.code = "VALIDATION_FAILED";
    out.message = detail
      .map((d) => {
        if (!isRecord(d)) return "";
        const msg = typeof d.msg === "string" ? d.msg : "";
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean)
      .join("; ");
  } else if (isRecord(detail)) {
    out.detail = detail;
    out.code = typeof detail.code === "string" ? detail.code : "";
    out.message = typeof detail.message === "string" ? detail.message : "";
  } else {
    // A JSON body with no `detail` (some non-Loom handler). Only a top-level
    // `message`/`error` string is worth showing.
    const m = body.message ?? body.error;
    if (typeof m === "string") out.message = m;
    if (typeof body.code === "string") out.code = body.code;
  }
  return out;
}

/** Build the `ApiError` for a non-2xx `Response`. Reads the body once. */
export async function apiErrorFromResponse(r: Response): Promise<ApiError> {
  let text = "";
  try {
    text = await r.text();
  } catch {
    /* body unreadable — status alone */
  }
  const parsed = parseErrorBody(text);
  return new ApiError({
    status: r.status,
    code: parsed.code,
    message: parsed.message,
    requestId: r.headers.get("X-Request-Id") ?? "",
    detail: parsed.detail,
    retryAfter: Number(r.headers.get("Retry-After")) || 0,
  });
}

/** The line for a write that committed while only Loom's search copy of it lags
 *  (`PUBLICATION_PENDING`, `server/core/pg_lease.py`). It names no verb (a delete answers it
 *  too); the page may still show the old state, so it says what to do about that, and
 *  nothing else: the server's words name its tables. Here, not in errorWords, so the toast
 *  says it without the lazy wording chunk. */
export const PUBLICATION_PENDING =
  "Done, but lists and search have not caught up yet, so this page may still show the old state. Don't send it once more; reload in a moment to see the result.";

/** A write that committed with only its search copy pending: callers treat it as done. */
export function isPublicationPending(err: unknown): boolean {
  return err instanceof ApiError && err.code === "PUBLICATION_PENDING";
}

/** The line for a call whose connection dropped before its answer came: a write may still
 *  have committed. Here, not in errorWords, so the toast keeps it without the lazy chunk. */
export const LOST_CONNECTION =
  "Lost connection to Loom mid-request. The work may still have committed on the server — re-check the list before retrying.";

/** The browsers' own words for a call that never got its answer ("Failed to fetch",
 *  "Load failed", "NetworkError when attempting…", Chrome's "network error" on a cut body). */
export function isLostConnection(text: string): boolean {
  return /^(failed to fetch|fetch failed|load failed|networkerror|network error)\b/i.test(text);
}

/** The tail for a write a failure may have left behind. */
export const MAY_HAVE_SAVED = "It may have been saved; check before trying again.";

/** The line for a call the dashboard stopped waiting for: a write may still finish. */
export const TIMED_OUT = "Loom didn't answer in time. It may still finish; check before trying again.";

/** What a write's toast keeps when the wording cannot load: the one sentence that stops a
 *  second write, for the failures that may have left the first one behind. */
export function writeWarning(err: unknown): string {
  const name = typeof err === "object" && err !== null && "name" in err ? String(err.name) : "";
  const raw = err instanceof Error ? err.message : String(err);
  if (isLostConnection(raw)) return LOST_CONNECTION;
  if (name === "TimeoutError") return TIMED_OUT;
  if (err instanceof ApiError && mayHaveSaved(err)) return MAY_HAVE_SAVED;
  return "";
}

/** Whether a failed write may have landed: an ok answer whose body could not be read, or a
 *  5xx not marked retryable (`handler_failure_detail`; a handler that raised partway, a
 *  proxy's 502 or 504), unless the refusal was over the deployment's setup, which comes
 *  before anything is written. One rule for the line and for the toast's fallback. */
export function mayHaveSaved(err: ApiError): boolean {
  if (err.detail.category === "configuration") return false;
  return err.code === "ANSWER_UNREADABLE" || (err.status >= 500 && err.detail.retryable !== true);
}
