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
