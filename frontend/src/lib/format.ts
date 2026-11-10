// Date / value formatters shared across views.

import { ApiError } from "./errors";

// One formatter per shape, built once. Each `toLocale*String` call constructs an
// Intl.DateTimeFormat; a page of a hundred cards built a hundred (44 ms of main thread at
// 4x CPU).
const _fmt = new Map<string, Intl.DateTimeFormat>();
function fmt(key: string, opts?: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  let f = _fmt.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(undefined, opts);
    _fmt.set(key, f);
  }
  return f;
}
/** Tests: the formatter instances this module holds, by shape. */
export function _formatterCacheSize(): number {
  return _fmt.size;
}

export function relativeTime(time: string | null | undefined): string {
  const d = parseChTime(time);
  if (!d) return "";
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  // Future timestamps: render "in Xh / in Xd". Used for expires_at, which
  // is always future for a live row — without this it would fall into the s<0 branch and
  // render as "just now" for every expiry preset.
  if (s < -1) {
    const a = -s;
    if (a < 60) return `in ${a}s`;
    if (a < 3600) return `in ${Math.floor(a / 60)}m`;
    if (a < 86400) return `in ${Math.floor(a / 3600)}h`;
    if (a < 604800) return `in ${Math.floor(a / 86400)}d`;
    return `on ${fmt("date").format(d)}`;
  }
  if (s < 0) return "just now"; // small clock skew
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 604800) return `${Math.floor(s / 86400)}d ago`;
  return fmt("date").format(d);
}

export function dayLabel(time: string): string {
  const d = parseChTime(time);
  if (!d) return "Unknown";
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today.getTime() - 86400000);
  const dDate = new Date(d.getTime());
  dDate.setHours(0, 0, 0, 0);
  const date =
    dDate.getFullYear() === today.getFullYear()
      ? fmt("month-day", { month: "short", day: "numeric" }).format(dDate)
      : fmt("month-day-year", { month: "short", day: "numeric", year: "numeric" }).format(dDate);
  if (dDate.getTime() === today.getTime()) return `Today · ${date}`;
  if (dDate.getTime() === yesterday.getTime()) return `Yesterday · ${date}`;
  return date;
}

/** A ClickHouse time ("YYYY-MM-DD hh:mm:ss[.fff…]", no zone, so UTC) or an ISO time, as a
 *  Date; null for nothing or anything that is not a time. */
export function parseChTime(s: string | null | undefined): Date | null {
  if (!s) return null;
  let t = s.trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(t)) t += "T00:00:00";
  t = t.replace(/^(\d{4}-\d{2}-\d{2}) (\d)/, "$1T$2");
  // DateTime64(6|9): Date reads three fraction digits at most.
  t = t.replace(/(\.\d{3})\d+/, "$1");
  // No zone: UTC.
  const d = new Date(/[+-]\d{2}:?\d{2}$|Z$/.test(t) ? t : t + "Z");
  return isNaN(d.getTime()) ? null : d;
}

/** The whole days left of the 14-day grace window for a memory deleted at `tombstonedAt`, a
 *  ClickHouse time and so UTC; null for no time or one that does not parse. */
export function daysLeftToRestore(tombstonedAt: string | null | undefined, now = Date.now()): number | null {
  const deleted = parseChTime(tombstonedAt);
  if (!deleted) return null;
  // A deletion stamped a moment ahead of this browser's clock is still day one.
  const age = Math.max(0, now - deleted.getTime());
  return Math.max(0, 14 - Math.floor(age / (24 * 3600 * 1000)));
}

/** "13 days left to restore", or "in grace window" when the time is unknown. */
export function restoreText(tombstonedAt: string | null | undefined): string {
  const daysLeft = daysLeftToRestore(tombstonedAt);
  if (daysLeft === null) return "in grace window";
  return `${daysLeft} day${daysLeft === 1 ? "" : "s"} left to restore`;
}

/** A list row's time (one rule for every list): "just now", "12m ago", "3h ago", "3d ago"
 *  within the week, then "Sep 20", or "Sep 20, 2025" in another year. `ui/ListTime`
 *  renders it with the absolute time in a tooltip. */
export function listTime(time: string | null | undefined): string {
  const d = parseChTime(time);
  if (!d) return "";
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s > -60 && s < 60) return "just now";
  if (s > 0 && s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s > 0 && s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s > 0 && s < 7 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return d.getFullYear() === new Date().getFullYear()
    ? fmt("month-day", { month: "short", day: "numeric" }).format(d)
    : fmt("month-day-year", { month: "short", day: "numeric", year: "numeric" }).format(d);
}

/** The absolute time behind a `listTime`, in UTC: "Sep 20, 2026, 14:32 UTC". */
export function listTimeTitle(time: string | null | undefined): string {
  const d = parseChTime(time);
  if (!d) return "";
  const f = fmt("utc-minute", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "UTC",
  });
  return `${f.format(d)} UTC`;
}

export function formatValue(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/** A wire value shown as a label, in sentence case ("assistant" → "Assistant"). */
export function sentenceCase(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// Made on first use, and only where the browser has one: this module loads on every page.
let graphemes: Intl.Segmenter | null | undefined;

function* characters(s: string): Iterable<string> {
  if (graphemes === undefined)
    graphemes =
      typeof Intl.Segmenter === "function" ? new Intl.Segmenter(undefined, { granularity: "grapheme" }) : null;
  if (!graphemes) {
    yield* s; // by code point
    return;
  }
  for (const { segment } of graphemes.segment(s)) yield segment;
}

/** `s` cut to at most `max` UTF-16 units, the ellipsis included, when it is longer, at the
 *  last boundary between the characters a reader sees (between code points where the
 *  browser has no Intl.Segmenter), so an emoji, a flag or an accented letter at the cut
 *  is never split; a string of `max` units or fewer comes back whole. */
export function truncate(s: string, max: number): string {
  if (!s || s.length <= max) return s;
  let kept = "";
  for (const c of characters(s)) {
    if (kept.length + c.length > max - 1) break;
    kept += c;
  }
  return `${kept.trimEnd()}…`;
}

/** A bounded preview of a value for a card: the first `max` characters of the string, or
 *  of a compact serialisation of the object — never a pretty-print of the whole value,
 *  which a list of a hundred cards paid for to show two lines each. */
export function previewValue(v: unknown, max = 320): string {
  if (v == null) return "";
  if (typeof v === "string") return v.length > max ? v.slice(0, max) : v;
  try {
    const s = JSON.stringify(v);
    return s.length > max ? s.slice(0, max) : s;
  } catch {
    return String(v).slice(0, max);
  }
}

/** Sentences for the server's error codes (`server/core/errors.py` and the
 *  `HTTPException` details in `server/main.py`), kept only where the code says more than
 *  its category or status would. The server's own message rides along after the sentence
 *  (it states the cause): a sentence ending in ":" introduces it; one ending in "."
 *  precedes it. No sentence says to try again: `retryTail` owns that, from the body. */
export const ERROR_CODE_PREFIXES: Record<string, string> = {
  AUTH_INVALID: "Sign-in rejected:",
  SIGNED_IN_ELSEWHERE: "Another tab signed in as someone else; reload this page.",
  AUTH_INSUFFICIENT: "Your role doesn't allow this:",
  IDENTITY_INCOMPLETE: "This token is scoped — the request has to name the namespace:",
  POLICY_FORBIDDEN: "Blocked by a policy:",
  TOKEN_NAME_TAKEN: "Name taken:",
  NOT_FOUND: "Not found — it may have been deleted, or be somewhere you can't read:",
  RESOURCE_NOT_FOUND: "Not found — it may have been deleted, or be somewhere you can't read:",
  NAMESPACE_NOT_FOUND: "That namespace doesn't exist:",
  NAMESPACE_NOT_ACCESSIBLE: "You don't have access to that namespace:",
  CHAR_LIMIT_EXCEEDED: "Too long:",
  PAYLOAD_TOO_LARGE: "Too large:",
  UNSUPPORTED_FILE: "Loom can't read this file type:",
  IDEMPOTENT_CONFLICT: "Already done — a request with this key was processed before.",
  BLACKLISTED: "Blocked by a privacy filter:",
  BLOB_GONE: "The original file is no longer stored, so it can't be downloaded or extracted.",
  RATE_LIMITED: "Too many requests.",
  OVERLOADED: "Loom is busy right now.",
  BACKEND_TIMEOUT: "Loom took too long to answer.",
  BACKEND_UNAVAILABLE: "Loom's database didn't answer.",
  CODED_WIDTH_MISMATCH: "The embedding model's width doesn't match the vector tables — see Settings › Models & keys.",
  SELF_SIGNUP_DISABLED: "Sign-up is off on this instance. Ask your operator.",
  INTERNAL_ERROR: "Loom hit an internal error. If it keeps happening, send the request id to your operator.",
  EMAIL_IN_USE: "That email belongs to another account:",
  NAMESPACE_CLAIMED: "That name is already taken:",
  NAMESPACE_TAKEN: "That name is already taken:",
  NAMESPACE_HAS_ADMIN: "This namespace already has an admin:",
  PASSWORD_LOGIN_SSO: "This org signs in with single sign-on:",
  SELF_SIGNUP_SSO: "This org signs in with single sign-on:",
  MODEL_CALL_REFUSED: "The model provider refused the call:",
  MODEL_CALLS_DISABLED:
    "Model calls are off for this org; an org admin can turn them on in Admin › Guardrails unless a platform admin locked them:",
  SANDBOX_UNAVAILABLE: "Adding repositories is turned off on this deployment:",
  SELF_SERVICE_OFF: "Adding repositories is turned off on this deployment:",
  REPO_EXISTS: "This repository is already indexed:",
  REPO_QUOTA: "The repository limit is reached:",
};

/** Upgrade in progress: says when it works again, so it takes no tail. */
const SCHEMA_PENDING = "Loom is being upgraded and this isn't ready yet. It works again once the upgrade finishes.";

/** Refusals whose check could not run answer with category `dependency` (server
 *  `unverified_class`): the caller may well be allowed, so no sentence about roles. */
const UNVERIFIED_CODES = new Set(["AUTH_INSUFFICIENT", "POLICY_FORBIDDEN", "NAMESPACE_NOT_ACCESSIBLE"]);

/** Whose move an error is (`category` on every coded body, server `classified`). `input`
 *  is left out: the server gives it to every 4xx it does not class otherwise (a missing
 *  row, a name clash), so the status says more. */
const CATEGORY_SENTENCES: Record<string, string> = {
  auth: "Not allowed:",
  capacity: "Loom is busy:",
  dependency: "A service Loom relies on didn't answer:",
  configuration: "This needs a change to Loom's setup:",
  internal: "Loom hit an internal error:",
};

/** When the body carried no code we know, the HTTP status still says what
 *  kind of failure it was, without naming a cause. */
function sentenceForStatus(status: number): string {
  if (status === 401) return "Sign-in rejected:";
  if (status === 403) return "Not allowed:";
  if (status === 404) return "Not found:";
  if (status === 400 || status === 422) return "Not accepted:";
  if (status === 413) return "Too large:";
  if (status === 429) return "Too many requests.";
  if (status >= 502 && status <= 504) return "Loom didn't answer in time:";
  if (status >= 500) return "Loom hit a server error:";
  return "Couldn't do that:";
}

/** What to do next, from the body alone: whether resending can work and when. Silent when
 *  the server's own words already say it. */
function retryTail(err: ApiError): string {
  if (/\b(retry\w*|again|resend\w*)\b/i.test(err.serverMessage)) return "";
  const category = typeof err.detail.category === "string" ? err.detail.category : "";
  const retryable = err.detail.retryable;
  if (category === "configuration") return "Trying again won't help until that's changed.";
  if (retryable === true) {
    const wait = Number(err.detail.retry_after_s ?? err.retryAfter);
    return wait > 0 ? `Try again in ${Math.ceil(wait)} s.` : "Try again in a moment.";
  }
  // Busy or a dependency, yet not retryable: the server's mark for a write that may have
  // landed before it failed (`handler_failure_detail`).
  if (retryable === false && (category === "capacity" || category === "dependency"))
    return "It may have been saved; check before trying again.";
  return "";
}

function joinSentence(friendly: string, message: string): string {
  const msg = message.trim();
  if (!msg) return friendly.endsWith(":") ? friendly.slice(0, -1) + "." : friendly;
  return `${friendly} ${msg}`;
}

/** A server message set after a full stop: capitalised and closed. */
function asSentence(msg: string): string {
  const t = msg.trim();
  const closed = /[.!?]$/.test(t) ? t : `${t}.`;
  return closed.charAt(0).toUpperCase() + closed.slice(1);
}

function withTail(text: string, tail: string): string {
  if (!tail) return text;
  return `${/[.!?]$/.test(text) ? text : `${text}.`} ${tail}`;
}

const LOST_CONNECTION =
  "Lost connection to Loom mid-request. The work may still have committed on the server — re-check the list before retrying.";
/** The same, for a caller that read rather than wrote (`bare`): nothing can have committed. */
const LOST_CONNECTION_READ = "Lost connection to Loom.";
const TIMED_OUT = "Loom didn't answer in time. It may still finish; check before trying again.";

/** Shown for anything thrown that is not a plain `Error`: the dashboard words its own
 *  refusals as plain Errors, while a TypeError, an AbortError or a library's own class
 *  carries text meant for a developer. */
const DASHBOARD_FAULT = "Something went wrong in the dashboard. Reload the page.";

export interface HumanizeOptions {
  /** Leave out the lead-in sentence: the caller supplies its own ("Couldn't delete x:"),
   *  so the line does not read "Couldn't delete x: Couldn't do that: …". */
  bare?: boolean;
}

/** One readable line for any thrown value. An `ApiError` maps by server code, then by its
 *  category, then by HTTP status, and says whether trying again can help — the raw
 *  response body never reaches the screen. Plain `CODE: detail` strings and bare network
 *  errors map too; a dashboard bug reads as one; any other value is shown as its text. */
export function humanizeError(err: unknown, opts: HumanizeOptions = {}): string {
  if (!err) return DASHBOARD_FAULT;
  if (err instanceof ApiError) return humanizeApiError(err, opts.bare === true);
  const raw = err instanceof Error ? err.message : String(err);
  // Raw network errors (connection drop, fetch aborted, etc.) come
  // back as bare strings like "Failed to fetch" / "fetch failed" /
  // "Load failed" / "NetworkError when attempting…". Distinguish "we
  // never reached the server" from "the server said no" — and warn that
  // the server may have committed the work even though we lost the response.
  if (/^(failed to fetch|fetch failed|load failed|networkerror)\b/i.test(raw)) {
    return opts.bare ? LOST_CONNECTION_READ : LOST_CONNECTION;
  }
  // A DOMException is not an Error in every engine, so the name is read off the object.
  const name = typeof err === "object" && "name" in err ? String(err.name) : "Error";
  // Every call is bounded (`AbortSignal.timeout`, api.ts): a slow server, not a bug here.
  if (name === "TimeoutError") return TIMED_OUT;
  if (name === "AbortError") return "Cancelled.";
  if (!raw || name !== "Error") return DASHBOARD_FAULT;
  const m = /^([A-Z][A-Z_]+):?\s*(.*)$/s.exec(raw);
  if (!m) return raw;
  const friendly = ERROR_CODE_PREFIXES[m[1]];
  if (!friendly) return raw;
  return joinSentence(friendly, m[2] ?? "");
}

/** The server's words (server/auth/bearer.py) for a call sent after the session ended in
 *  another tab took the token; a refusal of a token the call carried is `sessionEnded`. */
const SESSION_GONE = /missing bearer token/;

/** The code's own sentence, when it says more than its category or status. */
function codeSentence(err: ApiError): string | undefined {
  const category = typeof err.detail.category === "string" ? err.detail.category : "";
  if (UNVERIFIED_CODES.has(err.code) && category === "dependency") return "Loom couldn't check your access just now.";
  return ERROR_CODE_PREFIXES[err.code];
}

function leadFor(err: ApiError): string {
  const category = typeof err.detail.category === "string" ? err.detail.category : "";
  return codeSentence(err) ?? CATEGORY_SENTENCES[category] ?? sentenceForStatus(err.status);
}

function humanizeApiError(err: ApiError, bare: boolean): string {
  // A page that read while its session was away says so plainly (the dashboard's card
  // offers the sign-in), not as a rejected credential. Behind a proxy, or with auth off,
  // there is no session here to sign in to: the server's words stand.
  if (
    err.status === 401 &&
    !window.__LOOM_PROXY_AUTH &&
    !window.__LOOM_AUTH_DISABLED &&
    (err.sessionEnded || SESSION_GONE.test(err.serverMessage))
  )
    return "Your session ended — sign in again.";
  if (err.code === "SCHEMA_PENDING") return SCHEMA_PENDING;
  const lead = leadFor(err);
  let text: string;
  if (err.serverMessage && bare) {
    // The caller's lead-in replaces a generic one; a code's sentence stays, since it
    // carries the next step ("Your role doesn't allow this.").
    const own = codeSentence(err);
    text = own ? `${own.replace(/:$/, ".")} ${asSentence(err.serverMessage)}` : err.serverMessage;
  } else if (err.serverMessage) text = joinSentence(lead, err.serverMessage);
  // With no message the code is all the server said: kept for support, never the sentence.
  else if (err.code) text = `${lead.replace(/[:.]$/, "")} (${err.code}).`;
  else text = joinSentence(lead, "");
  return withTail(text, retryTail(err));
}

/** An inline "Couldn't load …:" line's reason: the bare words and, when the failure
 *  carried one, the request id, since the sentence may ask the reader to send it. */
export function errorLine(err: unknown): string {
  const rid = errorRequestId(err);
  return rid ? `${humanizeError(err, { bare: true })} Request id ${rid}.` : humanizeError(err, { bare: true });
}

/** The request id to print under an error for support, or "" when the
 *  failure carried none (a network error, a non-ApiError throw). */
export function errorRequestId(err: unknown): string {
  return err instanceof ApiError ? err.requestId : "";
}

export function timeOfDay(time: string): string {
  const d = parseChTime(time);
  if (!d) return "";
  return fmt("time", { hour: "2-digit", minute: "2-digit" }).format(d);
}

/** Map agent sentinels to human-readable labels. `*` is the wildcard
 *  Loom uses for cross-agent search (SPA cross-agent listMemories,
 *  fan-out memory.search). `-` is the placeholder when a caller
 *  didn't pass an agent field (admin / caller-API endpoints, unauth
 *  requests). Returns the raw value for any other agent. */
export function displayAgent(agent: string | null | undefined): string {
  if (!agent) return "—";
  if (agent === "*") return "Cross-agent search";
  if (agent === "-") return "No agent (admin / me)";
  return agent;
}

export const SENTINEL_AGENTS = new Set(["*", "-"]);

/** Service-account subjects are minted as `sa:<client>-<6 hex>` (see
 *  server/identity/app/me_service_accounts.py). Nobody should read that string on a card:
 *  `sa:reporter-a1b2c3` → "Reporter bot". Human subjects (emails, the self-signup
 *  `personal-*` form) come back unchanged. */
export function isServiceSubject(subject: string | null | undefined): boolean {
  return !!subject && subject.startsWith("sa:");
}

/** A namespace `kind` the server marks as a bench / QA corpus rather than
 *  a person's memory namespace. Only the kinds the server sends
 *  (`NamespaceInfo.kind`); an absent kind is not a system namespace. */
export function isSystemKind(kind: string | null | undefined): boolean {
  return kind === "qa" || kind === "bench";
}

export function displaySubject(subject: string | null | undefined): string {
  if (!subject) return "—";
  if (!isServiceSubject(subject)) return subject;
  const name = subject.slice("sa:".length).replace(/-[0-9a-f]{6,}$/i, "");
  const words = name
    .split(/[-_]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1));
  return words.length ? `${words.join(" ")} bot` : "Service account";
}

/** A byte count in B / KB / MB, one decimal above a kilobyte. */
export function formatBytesShort(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// One Intl.NumberFormat for every integer the dashboard prints. `Number.toLocaleString()`
// builds a formatter per call — 19 ms at 4x on the Repositories facets, twice per Profile
// row.
const _int = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
export function fmtInt(n: number | null | undefined): string {
  return _int.format(Number(n ?? 0));
}

/** A full local date and time for tooltips: the cached formatter, not a
 *  `Date.toLocaleString()` that builds one per row. */
export function absoluteTime(time: string | null | undefined): string {
  const d = parseChTime(time);
  return d ? fmt("abs", { dateStyle: "medium", timeStyle: "short" }).format(d) : (time ?? "");
}

/** A `code.refresh` caller line without the fields the caller left empty (`expected_head=`). */
export function refreshDetail(detail: string): string {
  return (detail || "")
    .split(/\s+/)
    .filter((part) => part && !part.endsWith("="))
    .join(" ");
}

/** What a card shows for a high row, which the list sends without its value. */
export const SENSITIVE_LABEL = "Sensitive: open it to read";
/** The same, where the view cannot open the row itself. */
export const SENSITIVE_READ_BY_KEY = "Sensitive: read it by key in Memories";
