// Thin fetch wrapper. In proxy mode, an upstream reverse proxy adds
// Authorization on every request; in paste mode, we use the token from
// localStorage.

import { noteCallEnd, noteCallStart } from "./lib/vitalsCounter";
import { claimsFromToken } from "./lib/boot";
import { ApiError, apiErrorFromResponse } from "./lib/errors";
export { ApiError } from "./lib/errors";
// The two code-plane shapes the shell reads; the plane itself lives in api/code.ts and rides with the Repositories view.
import type { CodeIndexStatus, CodeRepoSummary } from "./api/code";

const PROXY_MODE: boolean = !!window.__LOOM_PROXY_AUTH;
const KEY_TOKEN = "loom_token_v1";
const KEY_NAMESPACE = "loom_namespace_v1";

export function getSavedNamespace(): string | null {
  return localStorage.getItem(KEY_NAMESPACE);
}

export function setSavedNamespace(ns: string): void {
  // Auth-disabled mode serves exactly one namespace: the SPA identity is
  // pinned to LOOM_DEFAULT_NAMESPACE and the MCP resolver uses the same
  // setting, so there is no pick to save — and persisting one to
  // `loom.user_state` would silently retarget agent writes away from the dashboard. Belt
  // to the picker's suspenders (it doesn't render a switcher in this mode).
  if (isAuthDisabled()) return;
  localStorage.setItem(KEY_NAMESPACE, ns);
  // Persist server-side too so MCP clients pick up the same default the
  // next time the user calls a Loom tool. Failure is non-fatal: the
  // localStorage cache keeps the browser session working.
  void post("/v1/me/set-default-namespace", { namespace: ns }).catch(() => {
    /* fail-quiet */
  });
}

/** Forget the saved pick in this browser. The server-side default stays: no call clears it. */
export function forgetSavedNamespace(): void {
  localStorage.removeItem(KEY_NAMESPACE);
}

// Auth-disabled mode: when LOOM_AUTH_REQUIRED=false, the server accepts
// all calls without auth. The SPA skips the TokenGate and synthesises an
// identity for the server-configured default namespace.
const _AUTH_DISABLED: boolean = !!window.__LOOM_AUTH_DISABLED;

export function isAuthDisabled(): boolean {
  return _AUTH_DISABLED;
}

// The namespace an auth-disabled deployment serves (LOOM_DEFAULT_NAMESPACE,
// injected by the dashboard's boot snippet). "local" is the default for `npm run dev`
// against the static globals.
const _DEFAULT_NAMESPACE: string = window.__LOOM_DEFAULT_NAMESPACE || "local";

export function defaultNamespace(): string {
  return _DEFAULT_NAMESPACE;
}

function token(): string | null {
  if (PROXY_MODE) return null;
  return localStorage.getItem(KEY_TOKEN);
}

export function authHeader(): Record<string, string> {
  if (PROXY_MODE) return {};
  const t = token();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export function setToken(jwt: string): void {
  // Strip all whitespace before persisting — a pasted JWT can carry
  // internal newlines/spaces that make the Authorization header invalid.
  const cleaned = jwt.replace(/\s+/g, "");
  localStorage.removeItem(KEY_SIGNED_OUT);
  localStorage.setItem(KEY_TOKEN, cleaned);
}

// Set by a sign-out in this browser and cleared by the next sign-in: until then, a tab that
// loses the token or reloads goes to a plain sign-in, not "your session ended", whose SSO
// gate would sign the person back in.
const KEY_SIGNED_OUT = "loom_signed_out_v1";

/** Whether the person signed out in this browser (in any tab) and has not signed in since. */
export function signedOut(): boolean {
  return localStorage.getItem(KEY_SIGNED_OUT) !== null;
}

export function clearToken(): void {
  localStorage.removeItem(KEY_TOKEN);
}

/** Call `fn` with each token another tab of this origin stores (a sign-in there), or null
 *  when one drops it or signs out, and the token stored before; the returned function
 *  stops listening. */
export function onTokenStored(fn: (token: string | null, before: string | null) => void): () => void {
  const listener = (e: StorageEvent) => {
    // A browser fires it only on a change. A sign-out elsewhere counts even when this tab's
    // token was already gone (its own removal, when there was one, follows as a token
    // event), and so does a clear of the whole storage (no key), which drops the token.
    if (e.key === KEY_TOKEN || e.key === null || (e.key === KEY_SIGNED_OUT && !!e.newValue && !token()))
      fn(token(), e.oldValue);
  };
  window.addEventListener("storage", listener);
  return () => window.removeEventListener("storage", listener);
}

// The person this page belongs to: boot's whoami answer (App keeps it in step).
let pageOwner: WhoAmI | null = null;
export function setPageOwner(owner: WhoAmI | null): void {
  pageOwner = owner;
}

/** Whether `t` (none, or one an auth-off install ignores, counts) is the page's person's. */
export function ownsToken(t: string | null): boolean {
  if (!t || !pageOwner?.subject || isAuthDisabled()) return true;
  const c = claimsFromToken(t);
  // Claims it cannot read name no one: the server's answer to that token decides.
  return !c || (c.subject === pageOwner.subject && (c.org ?? "") === (pageOwner.org ?? ""));
}

// The app's answer to a stored token the server refuses (expired or revoked): the session
// ends (a card over the page, or the gate while booting). Registered by App, so this
// module stays free of the store.
let signInRejected: (() => void) | null = null;
export function onSignInRejected(fn: () => void): void {
  signInRejected = fn;
}

/** Sign out: forget the stored token here at once, and end its session on the server
 *  when it belongs to one (the dashboard's SSO token, or a `loom login` token pasted in).
 *  The revocation is sent without waiting for it: `keepalive` lets it finish across the
 *  page reload that follows, and it gives up after a few seconds. A token with no
 *  sign-in session (a minted one, maybe a CI job's too) is only forgotten here. Returns
 *  false when another person's token is the stored one (another tab signed them in a
 *  moment ago): it is not this page's to end, and the reload that follows opens their page. */
export function signOut(): boolean {
  const t = token();
  if (!ownsToken(t)) return false;
  localStorage.setItem(KEY_SIGNED_OUT, "1");
  clearToken();
  if (!t || !claimsFromToken(t)?.session) return true;
  fetch("/oauth/revoke", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ token: t, client_id: "loom-dashboard" }),
    keepalive: true,
    signal: AbortSignal.timeout(5000),
  }).catch(() => {
    // Offline, too slow or the route is absent: the token is gone from this browser anyway.
  });
  return true;
}

// Presence check that seeds auth state at boot: if a token is already stored, the SPA can
// skip painting the login gate before whoami resolves (otherwise a refresh flashes
// TokenGate, then snaps back to the app).
export function hasToken(): boolean {
  return typeof localStorage !== "undefined" && !!localStorage.getItem(KEY_TOKEN);
}

/** The stored token itself, for boot to read its own claims; null in proxy mode or when
 *  none is stored. Never logged, never sent anywhere but the Authorization header. */
export function storedToken(): string | null {
  return token();
}

export function isProxyMode(): boolean {
  return PROXY_MODE;
}

export interface WhoAmI {
  authenticated: boolean;
  subject?: string;
  org?: string;
  namespaces?: string[];
  roles?: string[];
  is_service_account?: boolean;
  kid?: string;
  exp?: number;
  iat?: number;
}

/** Who the stored token signs in as; with `asToken`, who that token would, asked before
 *  anything is stored. */
export async function whoami(asToken?: string): Promise<WhoAmI> {
  const headers = asToken ? { Authorization: `Bearer ${asToken}` } : authHeader();
  const r = await fetch("/v1/whoami", { headers, signal: boundedSignal() });
  // A refused token answers 200 with authenticated=false. Anything else not ok (a replica
  // whose keys are still loading answers 503) says nothing about the token: it is kept.
  // Behind a proxy a refusal is the proxy's own 401 or 403: signed out, as before.
  if (PROXY_MODE && (r.status === 401 || r.status === 403)) return { authenticated: false };
  if (!r.ok) throw await apiErrorFromResponse(r);
  return readJson(r);
}

export interface MemoryListEntry {
  memory_key: string;
  memory_type: string; // semantic/episodic/procedural/preference + custom types
  confidence: number;
  version: number;
  created_at: string;
  // Last write to this memory_key.
  updated_at?: string | null;
  expires_at: string | null;
  // When set, this row is soft-deleted (in 14-day grace window).
  tombstoned_at?: string | null;
  pinned: boolean;
  tags: string[];
  // Writer identity, so cross-agent list views can re-issue memory.get
  // with the identity that wrote each row. Empty string = caller should
  // fall back to its current identity.agent.
  agent?: string;
  user_id?: string;
  session_id?: string;
  value?: unknown;
  // Provenance, e.g. "conversation:<thread_id>" for seeder-promoted facts.
  source_trace?: string;
  // Attestation: who verified this row and whether that still holds.
  verified_by?: string;
  verification?: VerificationState;
  value_truncated?: boolean;
  content_sha256?: string;
  char_limit?: number | null;
  // "" | low | medium | high. A high row's value is not in the list: memory.get reads it.
  sensitivity?: string;
}

/** human | process: attested and unchanged since. stale: attested, then
 *  edited. none: never attested. Derived server-side against the live hash. */
export type VerificationState = "human" | "process" | "stale" | "none";

export interface MemoryListResponse {
  entries: MemoryListEntry[];
  truncated: boolean;
  // Total rows in this namespace scope, ignoring the page `limit`. Lets the
  // UI show "N of TOTAL" instead of "N of <page cap>".
  total: number;
  // Of `total`, how many were created since UTC midnight today.
  today: number;
  // The namespace's newest memory version; send it back as `known_version` and an
  // unchanged namespace answers `unchanged: true` with no rows, total or today. A decimal
  // string: the value is above 2^53, which a JS number would round.
  namespace_version?: string;
  unchanged?: boolean;
  // Inputs this server does not know (a release behind), named rather than refused.
  ignored_args?: string[];
}

export interface Identity {
  org: string;
  namespace: string;
  agent: string;
  /** The user and session a row is filed under, when the inspector reads a row it was
   *  opened on by its whole writer identity; absent reads the caller's own (`-`). */
  user_id?: string;
  session_id?: string;
}

// In-flight tracking — every active request() bumps the counter; the UI
// subscribes via subscribeInflight() to drive the top progress bar.
let _inflight = 0;
const _inflightListeners = new Set<(n: number) => void>();
function _bump(delta: number): void {
  _inflight = Math.max(0, _inflight + delta);
  _inflightListeners.forEach((fn) => fn(_inflight));
}
export function subscribeInflight(fn: (n: number) => void): () => void {
  _inflightListeners.add(fn);
  fn(_inflight);
  return () => {
    _inflightListeners.delete(fn);
  };
}

/** Optional per-call plumbing shared by the read wrappers below. `signal`
 *  lets a view cancel a read its effect no longer wants; omitting it keeps every existing
 *  call's shape. */
export interface RequestOpts {
  signal?: AbortSignal;
}

/** No request waits forever. A caller's own signal wins; otherwise the request aborts
 *  after 60 s so a stalled server surfaces as an error instead of a spinner that never
 *  clears. */
const REQUEST_TIMEOUT_MS = 60_000;

/** The bound for a call that runs a whole erasure, clear or consolidation inside it: up to
 *  the shipped proxy's 600 s read timeout. */
export const LONG_CALL_MS = 10 * 60_000;
export function boundedSignal(signal?: AbortSignal): AbortSignal | undefined {
  if (signal) return signal;
  try {
    return typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
      ? AbortSignal.timeout(REQUEST_TIMEOUT_MS)
      : undefined;
  } catch {
    return undefined;
  }
}

/** The dashboard's consumer: usage reports leave out a person's reads through it
 *  (`namespace.audit` and `namespace.summary` still count a change the person makes). */
export const UI_CONSUMER = "loom-ui";

// A registry op's path, `/v1/<op>`: its input takes `consumer`. The other routes
// (`/v1/admin/...`, `/v1/me/...`) have their own bodies, and some refuse an unknown field.
const OP_PATH = /^\/v1\/[^/]+$/;

function asDashboardCall(path: string, body: unknown): unknown {
  if (!OP_PATH.test(path) || body === null || typeof body !== "object" || Array.isArray(body)) return body;
  // A body's own `consumer` is kept: some ops take it as an input (a cost summary's filter).
  return { consumer: UI_CONSUMER, ...(body as Record<string, unknown>) };
}

/** The one request path every authenticated call rides: the inflight counter, the view's
 *  call count, the bounded signal, and a typed ApiError (status, server code, message,
 *  request id) for any non-2xx — never a "403: {json}" string. Answers the ok Response. */
async function request(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  headers: Record<string, string> = {},
): Promise<Response> {
  _bump(+1);
  noteCallStart(); // the view's call count and span
  try {
    return await send(method, path, body, signal, false, headers);
  } finally {
    _bump(-1);
    noteCallEnd();
  }
}

async function send(
  method: "GET" | "POST",
  path: string,
  body: unknown,
  signal: AbortSignal | undefined,
  retried: boolean,
  headers: Record<string, string>,
): Promise<Response> {
  const sent = token();
  // Another tab may have stored another person's token a moment ago, before this one heard
  // of it (and reloads): this page's call never carries it.
  if (!ownsToken(sent)) {
    // Its words are the ended session's (sessionEnded): no message of its own is shown.
    const err = new ApiError({ status: 401 });
    err.sessionEnded = true;
    throw err;
  }
  // A file goes as its bytes (`knowledge.upload`); any other body is the op's JSON.
  const raw = body instanceof Blob;
  const r = await fetch(
    path,
    method === "POST"
      ? {
          method,
          headers: {
            "Content-Type": raw ? "application/octet-stream" : "application/json",
            ...headers,
            ...authHeader(),
          },
          body: raw ? body : JSON.stringify(asDashboardCall(path, body)),
          signal: boundedSignal(signal),
        }
      : { method, headers: { ...headers, ...authHeader() }, signal: boundedSignal(signal) },
  );
  if (r.status === 401 && sent) {
    // The first refusal of the stored token ends the session, before any await, so
    // concurrent ones end it once.
    const ending = sent === token();
    if (ending) signInRejected?.();
    // A single-user install has no sign-in: the stale token is gone (this refusal or a
    // concurrent one dropped it), so the call goes again without it.
    if (isAuthDisabled() && !retried) return send(method, path, body, signal, true, headers);
    // A call raced by another tab's sign-in is not sent again under a token this tab has
    // not checked: Retry sends it, and the error says so instead of "sign in again".
    if (!ending && token() && ownsToken(token())) throw new ApiError({ status: 401, code: "SIGNED_IN_ELSEWHERE" });
    const err = await apiErrorFromResponse(r);
    err.sessionEnded = true;
    throw err;
  }
  if (!r.ok) throw await apiErrorFromResponse(r);
  return r;
}

/** An ok answer's JSON body. A body that is not JSON (a proxy's page in place of Loom's
 *  answer) is an ApiError that says so, not the parser's words: the dashboard passes a
 *  SyntaxError through only for text the person typed or a file they picked. */
async function readJson<T>(r: Response): Promise<T> {
  try {
    return (await r.json()) as T;
  } catch (e) {
    // Only the parser's refusal is a body that is not JSON; anything else (a cut body, an
    // abort) keeps its own name. A DOMException is not an Error in every engine.
    if (typeof e !== "object" || e === null || !("name" in e) || e.name !== "SyntaxError") throw e;
    // The status was ok, so a write may well have committed: errorWords says so to a writer.
    throw new ApiError({ status: r.status, code: "ANSWER_UNREADABLE", requestId: r.headers.get("X-Request-Id") ?? "" });
  }
}

export async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return readJson(await request("POST", path, body, signal));
}

/** POST JSON and answer the ok Response unread, for a body that streams (the assistant's
 *  chat). `signal` is required: a stream is bounded by its caller, not the 60 s default.
 *  The request counts as done for the progress bar and the view's call span once its
 *  headers arrive: the caller shows that the body is still coming in. */
export async function postStreaming(
  path: string,
  body: unknown,
  headers: Record<string, string>,
  signal: AbortSignal,
): Promise<Response> {
  return request("POST", path, body, signal, headers);
}

/** POST a file's bytes as the body (`/v1/knowledge.upload?...`), parsed as JSON. Pass a
 *  `signal` for a large file: without one the call is bounded like any other. */
export async function postFile<T>(path: string, file: Blob, signal?: AbortSignal): Promise<T> {
  return readJson(await request("POST", path, file, signal));
}

/** A GET read on the shared request path, parsed as JSON. */
export async function getJson<T>(path: string, opts: RequestOpts = {}): Promise<T> {
  return readJson(await request("GET", path, undefined, opts.signal));
}

/** A GET whose body is not JSON (a download): the ok Response, for the caller to read. */
export async function getResponse(path: string, opts: RequestOpts = {}): Promise<Response> {
  return request("GET", path, undefined, opts.signal);
}

/** The namespace's memory total, and nothing else: `memory.list` with `count_only`, two
 *  cached counts and no page — asking for `limit 1` instead would cost the whole page
 *  statement. */
export async function countMemories(id: Identity, opts: RequestOpts = {}): Promise<number> {
  const r = await post<{ total?: number | null }>(
    "/v1/memory.list",
    { ...id, agent: "*", limit: 1, count_only: true },
    opts.signal,
  );
  return r.total ?? 0;
}

export async function listMemories(
  id: Identity,
  limit: number = 200,
  opts: {
    include_tombstoned?: boolean;
    include_profile?: boolean;
    source_trace?: string;
    offset?: number;
    known_version?: string;
    fields?: string[];
    previewChars?: number;
    // Only rows whose value or key holds this text, any case.
    contains?: string;
    // Only rows carrying every one of these tags, in any case.
    tags?: string[];
    // Only rows this agent wrote; every agent's rows when unset.
    agent?: string;
  } & RequestOpts = {},
): Promise<MemoryListResponse> {
  // Without an agent the Memories view shows every agent's rows, so it sends `agent=*` to
  // drop the server's per-agent filter.
  const out = await post<MemoryListResponse>(
    "/v1/memory.list",
    {
      ...id,
      agent: opts.agent || "*",
      limit,
      ...(opts.offset ? { offset: opts.offset } : {}),
      include_tombstoned: opts.include_tombstoned ?? false,
      ...(opts.source_trace ? { source_trace: opts.source_trace } : {}),
      // a poll sends the version it has; unchanged → no rows
      ...(opts.known_version ? { known_version: opts.known_version } : {}),
      // A page asks for the fields it renders and the preview length it shows; the answer
      // carries nothing else.
      ...(opts.fields ? { fields: opts.fields } : {}),
      ...(opts.previewChars ? { preview_chars: opts.previewChars } : {}),
      ...(opts.contains ? { contains: opts.contains } : {}),
      // Tags as typed; the server matches them in any case. Two or more must all match (a
      // build that predates `tags_every` matches any one, and the page then requires every tag
      // itself).
      ...(opts.tags?.length ? { tags: opts.tags, ...(opts.tags.length > 1 ? { tags_every: true } : {}) } : {}),
    },
    opts.signal,
  );
  // Profile sections have their own /profile view — exclude from the
  // generic Memories list by default. Pass include_profile=true to opt in.
  if (!opts.include_profile) {
    return {
      ...out,
      entries: (out.entries || []).filter((e) => e.memory_type !== "profile"),
    };
  }
  return out;
}

/** Bring a row back live: undo a tombstone within the 14-day grace window, or undo a
 *  supersession (a row Loom retired on its own, or a later write replaced). Returns
 *  restored=true on success, expired=true if the delete window has closed, restored=false
 *  (no-op) if the row was already live or doesn't exist. `from_status` says what it was
 *  restored from ("tombstoned" or "superseded"; empty on a no-op). user_id / session_id
 *  complete the row's identity tuple. */
export async function restoreMemory(
  id: Identity,
  key: string,
  opts: {
    user_id?: string;
    session_id?: string;
    /** Restore only a row still retired behind this memory id; otherwise restored=false. */
    if_superseded_by?: string;
    /** Restore only a row still retired, whatever it retired behind; never undo a delete. */
    only_if_superseded?: boolean;
  } = {},
): Promise<{
  restored: boolean;
  memory_id?: string | null;
  version?: number | null;
  expired?: boolean;
  from_status?: "tombstoned" | "superseded" | "";
}> {
  return post("/v1/memory.restore", {
    ...id,
    user_id: opts.user_id ?? "-",
    session_id: opts.session_id ?? "-",
    memory_key: key,
    ...(opts.if_superseded_by ? { if_superseded_by: opts.if_superseded_by } : {}),
    ...(opts.only_if_superseded ? { only_if_superseded: true } : {}),
  });
}

/** Wire shape returned by /v1/memory.get. `found=false` means the row
 *  doesn't exist; gate on it before reading the other fields. */
export interface MemoryDetail {
  found: boolean;
  memory_id?: string;
  value: unknown;
  memory_type: string | null;
  confidence: number | null;
  version: number | null;
  pinned: boolean;
  created_at: string | null;
  updated_at?: string | null;
  expires_at?: string | null;
  agent?: string;
  user_id?: string;
  session_id?: string;
  source_trace?: string;
  /** How the content entered memory: asserted | extracted | observed | derived |
   *  imported | external. */
  source_class?: string;
  /** The resource the fact was lifted from; "" when none. */
  source_resource_id?: string;
  /** '' | low | medium | high. */
  sensitivity?: string;
  /** SHA-256 of the live value; a write sends it as if_match_sha256. */
  content_sha256?: string;
  /** When the fact became true, and the closing bound of its validity; null when unset. */
  valid_at?: string | null;
  valid_to?: string | null;
  /** Recall credits: reads from a reader that posts outcomes, the ranking signal. */
  recall_count?: number;
  last_recalled_at?: string | null;
  /** Agent reads that returned the key, credited or not (searches, context packs, asks);
   *  only with include_recall_breakdown. */
  served_count?: number | null;
  last_served_at?: string | null;
  /** Agent reads of the key per named agent, last 30 days. */
  recall_by_agent?: Array<{ agent: string; count: number; last_at: string | null }>;
  tags?: string[];
  /** Entity names on the row (memory.get returns them; the drill-down into the Graph
   *  starts from one). */
  entities?: string[];
  status?: string;
  /** For a `status='superseded'` row, the memory_id of the row it retired behind. */
  superseded_by?: string;
  /** Server-enforced cap on the stringified value; NULL = no cap. The
   *  Inspector reads this to preserve the cap when editing the value,
   *  since the write path is write-through. */
  char_limit?: number | null;
  verified_by?: string;
  verified_at?: string | null;
  verification?: VerificationState;
  /** The lock in force (memory.lock): `read_only` | `no_delete`; "" when unlocked or lapsed. */
  lock_mode?: string;
  lock_reason?: string;
  /** When the lock lapses; null when open-ended. */
  lock_until?: string | null;
  locked_by?: string;
  /** On the wire only for a `status='duplicate'` row — the key that answers for it (a
   *  hash-collapsed duplicate, or the destination of a `memory.move` redirect). Absent
   *  otherwise. */
  canonical_key?: string;
  /** Only on a `follow: true` read — the key that was asked for when the canonical was
   *  served instead. */
  resolved_from?: string;
  /** The code the memory is linked to; `symbol` is "" for a file link. */
  code?: CodeLink[];
}

export interface CodeLink {
  code_namespace: string;
  path: string;
  symbol: string;
  state: "fresh" | "moved" | "changed" | "unresolved" | "missing";
  changed_since: string;
  /** The `revise` policy's corrected claim for the code as it is now; "" when none. */
  suggestion?: string;
}

export async function getMemory(
  id: Identity,
  key: string,
  opts: { include_recall_breakdown?: boolean } & RequestOpts = {},
): Promise<MemoryDetail> {
  const body: Record<string, unknown> = { ...id, memory_key: key };
  if (opts.include_recall_breakdown) body["include_recall_breakdown"] = true;
  return post<MemoryDetail>("/v1/memory.get", body, opts.signal);
}

/** One memory by its org-wide id (`memory.get_by_id`), the shape a conflict's
 *  `conflict_with` names its peer by. `found` is false when no live row carries the id
 *  or the caller cannot read its namespace. */
export interface MemoryById {
  found: boolean;
  memory_id: string;
  namespace: string;
  memory_key: string;
  value: unknown;
  version: number | null;
  pinned: boolean;
  created_at: string | null;
  /** '' | low | medium | high; a high row is read by key in Memories, not shown here. */
  sensitivity?: string;
}

/** `omit_high_value`: a high row answers without its value, and no sensitive read is
 *  audited, for a view that only names the row. */
export async function getMemoryById(
  org: string,
  memoryId: string,
  opts: { omit_high_value?: boolean } & RequestOpts = {},
): Promise<MemoryById> {
  const body: Record<string, unknown> = { org, memory_id: memoryId };
  if (opts.omit_high_value) body["omit_high_value"] = true;
  return post<MemoryById>("/v1/memory.get_by_id", body, opts.signal);
}

/** POST /v1/memory.bulk_get — N keys in one CH round-trip. `agent: "*"` is the
 *  namespace-wide lookup the Memories list needs: the page mixes writers, and under the
 *  wildcard the server opens the unset user/session axes the same way memory.get does.
 *  `results` holds the hits keyed by memory_key; `missing` the keys with no live row. */
export async function bulkGetMemories(
  id: Identity,
  keys: string[],
  opts: RequestOpts = {},
): Promise<{ results: Record<string, MemoryDetail>; missing: string[] }> {
  if (keys.length === 0) return { results: {}, missing: [] };
  return post("/v1/memory.bulk_get", { ...id, memory_keys: keys }, opts.signal);
}

export async function setMemory(
  id: Identity,
  key: string,
  value: unknown,
  opts: {
    tags?: string[];
    memoryType?: MemoryListEntry["memory_type"];
    /** Seconds until the memory expires (auto-tombstone). Omit / pass
     *  null for "never expire". */
    ttlSeconds?: number | null;
    /** Force expires_at=NULL on the new row, overriding any per-tag
     *  retention default. Used by the "Never expire" affordance on a row
     *  that already has an inherited expiry. */
    clearExpiry?: boolean;
    /** Server-enforced cap on the stringified value. The write path is
     *  write-through, so callers editing a capped row must read the
     *  current char_limit and pass it back. 422 CHAR_LIMIT_EXCEEDED on
     *  overflow. */
    charLimit?: number | null;
  } = {},
): Promise<{ version: number; pinned: boolean }> {
  const body: Record<string, unknown> = {
    ...id,
    memory_key: key,
    value,
    tags: opts.tags || [],
    // Server defaults to "semantic" when omitted.
    ...(opts.memoryType ? { memory_type: opts.memoryType } : {}),
  };
  // Only send ttl_seconds when explicitly set: omitting it keeps the
  // previous expires_at on an update, or "never expire" on a fresh write.
  if (opts.ttlSeconds != null && opts.ttlSeconds > 0) {
    body.ttl_seconds = opts.ttlSeconds;
  }
  if (opts.clearExpiry) {
    body.clear_expiry = true;
  }
  if (opts.charLimit != null) {
    body.char_limit = opts.charLimit;
  }
  return post<{ version: number; pinned: boolean }>("/v1/memory.set", body);
}

/** The source classes a write may declare (`server.memory.domain.provenance`); the
 *  server derives the others from `source_trace`. */
const DECLARABLE_SOURCE_CLASSES = new Set(["derived", "external", "imported"]);

/** The identity of a row read with `getMemory`, for a write back to that row:
 *  memory.get may answer with another agent's or session's row, and a write under
 *  the caller's identity would land on a second row beside it. */
export function rowIdentity(id: Identity, row: MemoryDetail): Identity & { user_id: string; session_id: string } {
  return {
    ...id,
    agent: row.agent || id.agent,
    user_id: row.user_id || "-",
    session_id: row.session_id || "-",
  };
}

/** Pin a row read with `getMemory`, at `rowIdentity`. memory.pin writes a whole new
 *  version as memory.set does, so the row's fields go back with it; leaving
 *  ttl_seconds out keeps the row's expiry. */
export async function pinMemory(
  id: Identity,
  key: string,
  row: MemoryDetail,
): Promise<{ version: number; pinned: boolean }> {
  const body: Record<string, unknown> = {
    ...rowIdentity(id, row),
    memory_key: key,
    value: row.value,
    memory_type: row.memory_type,
    tags: row.tags,
    entities: row.entities,
  };
  if (row.confidence != null) body.confidence = row.confidence;
  if (row.sensitivity) body.sensitivity = row.sensitivity;
  if (row.source_trace) body.source_trace = row.source_trace;
  if (row.source_resource_id) body.source_resource_id = row.source_resource_id;
  if (row.source_class && DECLARABLE_SOURCE_CLASSES.has(row.source_class)) {
    body.source_class = row.source_class;
  }
  if (row.valid_at) body.valid_at = row.valid_at;
  if (row.valid_to) body.valid_to = row.valid_to;
  if (row.char_limit != null) body.char_limit = row.char_limit;
  // A change to the value since the read answers 409 instead of being reverted.
  if (row.content_sha256) body.if_match_sha256 = row.content_sha256;
  return post<{ version: number; pinned: boolean }>("/v1/memory.pin", body);
}

/** `id` may name the row's user and session (`rowIdentity`); they default to '-'. */
export async function unpinMemory(
  id: Identity,
  key: string,
): Promise<{ unpinned: boolean; reason: string; memory_id: string; version: number | null }> {
  return post("/v1/memory.unpin", { ...id, memory_key: key });
}

export interface VerifyResult {
  found: boolean;
  changed: boolean;
  verified_by: string;
  verified_at: string | null;
  verification: VerificationState;
  content_sha256: string;
}

/** Attest to one memory as the signed-in person, or revoke the attestation.
 *  The server derives the actor from the token (`human:<subject>`), so the
 *  UI never chooses it. Binds to the row's current content. */
export async function verifyMemory(
  id: Identity,
  key: string,
  opts: { revoke?: boolean; userId?: string; sessionId?: string } = {},
): Promise<VerifyResult> {
  return post<VerifyResult>("/v1/memory.verify", {
    org: id.org,
    namespace: id.namespace,
    agent: id.agent,
    user_id: opts.userId || "-",
    session_id: opts.sessionId || "-",
    memory_key: key,
    revoke: !!opts.revoke,
  });
}

/** Tombstone one memory.
 *
 *  Returns the server's answer instead of discarding it. `memory.delete` is
 *  idempotent: deleting a row that does not match the full identity tuple is a
 *  200 with `tombstoned: false`, not an error: a caller that only catches
 *  exceptions reports "Deleted" for a delete that did nothing. Check `tombstoned`. */
export async function deleteMemory(id: Identity, key: string): Promise<{ tombstoned: boolean; version: number }> {
  return post<{ tombstoned: boolean; version: number }>("/v1/memory.delete", {
    ...id,
    memory_key: key,
  });
}

/** POST /v1/memory.move: rename `fromKey` to `toKey`. The destination is written through
 *  memory.set; the source becomes a `status='duplicate'` redirect (`keepRedirect`,
 *  default) or is tombstoned. Mirrors
 *  `server/memory/api/models_move.py::MemoryMoveOutput`. */
export interface MemoryMoveResult {
  from_key: string;
  to_key: string;
  // True when the source row now redirects to the destination; false when
  // it was tombstoned (keep_redirect: false) or nothing was written (dry run).
  redirected: boolean;
  // The destination row (empty / 0 on a dry run).
  memory_id: string;
  version: number;
  content_sha256: string;
  // memory.set's advisories on the destination write, passed through.
  advisories: string[];
  dry_run: boolean;
  // Dry-run preview of the destination: its type and the first 200 chars.
  memory_type: string;
  value_preview: string;
}

export async function moveMemory(
  id: Identity,
  fromKey: string,
  toKey: string,
  opts: { keepRedirect?: boolean; dryRun?: boolean; user_id?: string; session_id?: string } = {},
): Promise<MemoryMoveResult> {
  return post<MemoryMoveResult>("/v1/memory.move", {
    ...id,
    ...(opts.user_id ? { user_id: opts.user_id } : {}),
    ...(opts.session_id ? { session_id: opts.session_id } : {}),
    from_key: fromKey,
    to_key: toKey,
    keep_redirect: opts.keepRedirect ?? true,
    dry_run: opts.dryRun ?? false,
  });
}

// ===== Datapacks / Grants =====

export type GrantRole = "viewer" | "writer" | "admin";

export interface GrantEntry {
  subject: string;
  role: GrantRole;
  /** 'active' = grant is in force; 'pending' = awaiting admin approval
   *  on a requires_approval namespace. */
  status: "active" | "pending";
  /** When set, the subject is queued for elevation to this role —
   *  `role` is current, `pending_role` is what an admin must approve. */
  pending_role?: GrantRole | null;
  pending_since?: string | null;
  granted_at: string;
  granted_by: string;
  /** The person's other spellings whose rows folded onto this one. */
  also?: string[];
}

export async function listGrants(
  id: Pick<Identity, "org" | "namespace">,
  opts: { limit?: number } & RequestOpts = {},
): Promise<{ rows: GrantEntry[] }> {
  return post<{ rows: GrantEntry[] }>(
    "/v1/grants.list",
    {
      org: id.org,
      namespace: id.namespace,
      limit: opts.limit ?? 100,
    },
    opts.signal,
  );
}

/** Every pending request in the namespaces the caller administers, org-wide, in one call
 *  — instead of a `grants.list` per administered namespace. */
export interface PendingApproval {
  namespace: string;
  kind?: string;
  subject: string;
  current_role: GrantRole;
  requested_role: GrantRole;
  pending_since: string | null;
}
export async function listPendingApprovals(
  org: string,
  namespace?: string,
  opts: RequestOpts = {},
): Promise<{ rows: PendingApproval[] }> {
  // A scoped token's transport gate needs a namespace in every body
  // (IDENTITY_INCOMPLETE otherwise); the handler reads org-wide regardless.
  const body: Record<string, unknown> = { org };
  if (namespace) body["namespace"] = namespace;
  return post<{ rows: PendingApproval[] }>("/v1/grants.pending_for_me", body, opts.signal);
}

export async function createGrant(
  id: Pick<Identity, "org" | "namespace">,
  subject: string,
  role: GrantRole,
  /** The server refuses a create below the subject's current role (409
   *  GRANT_WOULD_DOWNGRADE) unless `replace` is set. Only the explicit "change role"
   *  control passes it; "add member" never does, so re-adding an existing admin as writer
   *  cannot demote them. */
  opts: { replace?: boolean } = {},
): Promise<unknown> {
  return post("/v1/grants.create", {
    org: id.org,
    namespace: id.namespace,
    subject,
    role,
    granted_by: "dashboard",
    ...(opts.replace ? { replace: true } : {}),
  });
}

export async function revokeGrant(
  id: Pick<Identity, "org" | "namespace">,
  subject: string,
): Promise<{ revoked: boolean }> {
  return post<{ revoked: boolean }>("/v1/grants.revoke", {
    org: id.org,
    namespace: id.namespace,
    subject,
  });
}

export async function approveGrant(
  id: Pick<Identity, "org" | "namespace">,
  subject: string,
  decision: "approve" | "reject" = "approve",
  /** Approve with a different role than was asked for (a maintainer request answered as reader). Omit = as asked. */
  role?: GrantRole,
): Promise<{ decision: string; role: GrantRole | null }> {
  const body: Record<string, unknown> = { org: id.org, namespace: id.namespace, subject, decision };
  if (role) body.role = role;
  return post("/v1/grants.approve", body);
}

// ===== Connected agents =====

export interface ConnectedAgentRow {
  agent: string;
  last_seen: string;
  /** All-time earliest sighting of this agent in agent_events — flags
   *  genuinely-new connections vs. ones merely active in the window. */
  first_seen: string;
  ops: number;
  namespaces_touched: number;
  primary_namespace: string;
  cost_usd: number;
}

/** Per-agent rollup over agent_events. Filters out sentinels server-side
 *  so the UI only sees real clients. Empty namespace = org-wide. */
export async function listConnectedAgents(
  id: Pick<Identity, "org"> & { namespace?: string; window_hours?: number },
  opts: RequestOpts = {},
): Promise<{ rows: ConnectedAgentRow[]; window_hours: number }> {
  const body: Record<string, unknown> = {
    org: id.org,
    window_hours: id.window_hours ?? 24,
  };
  if (id.namespace) body["namespace"] = id.namespace;
  return post("/v1/agents.connected", body, opts.signal);
}

/** The sidebar badge's number — agents first seen inside the window that did something — as one aggregate, no rows. */
export async function countNewAgents(
  id: Pick<Identity, "org"> & { namespace?: string; window_hours?: number },
  opts: RequestOpts = {},
): Promise<number> {
  const body: Record<string, unknown> = {
    org: id.org,
    window_hours: id.window_hours ?? 24,
    count_only: true,
  };
  if (id.namespace) body["namespace"] = id.namespace;
  const r = await post<{ new_count?: number | null }>("/v1/agents.connected", body, opts.signal);
  return Number(r.new_count ?? 0);
}

// ===== Conflicts =====

export interface ConflictRow {
  memory_id: string;
  memory_key: string;
  memory_type: string;
  value: unknown;
  version: number;
  pinned: boolean;
  created_at: string;
  conflict_with: string[];
  content_sha256: string;
  // A high row comes without its value; the inspector reads it by key.
  sensitivity?: string;
}

export async function listConflicts(
  id: Pick<Identity, "org" | "namespace">,
  opts: { limit?: number; before_version?: number } & RequestOpts = {},
): Promise<{ rows: ConflictRow[]; next_before_version: number | null }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    limit: opts.limit || 50,
  };
  if (opts.before_version) body["before_version"] = opts.before_version;
  return post("/v1/conflicts.list", body, opts.signal);
}

/** The namespace's conflicted count. `fresh` counts now instead of answering from the
 *  replica's minute-long cache (the review queue; the sidebar badge takes the cache). */
export async function countConflicts(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts & { fresh?: boolean } = {},
): Promise<number> {
  const r = await post<{ total?: number | null }>(
    "/v1/conflicts.list",
    { org: id.org, namespace: id.namespace, count_only: true, ...(opts.fresh ? { fresh: true } : {}) },
    opts.signal,
  );
  return r.total ?? 0;
}

/** The conflicted memory keys of a namespace, for the cards' chips
 *  — the same operation with `keys_only`, one FINAL read of the key columns and no value
 *  column. */
export async function conflictKeys(id: Pick<Identity, "org" | "namespace">, opts: RequestOpts = {}): Promise<string[]> {
  const r = await post<{ keys?: string[] | null }>(
    "/v1/conflicts.list",
    { org: id.org, namespace: id.namespace, keys_only: true },
    opts.signal,
  );
  return r.keys ?? [];
}

export type ResolveDecision = "revise" | "contract" | "allow";

export interface AllowlistRow {
  memory_key_a: string;
  memory_key_b: string;
  allowed_at: string;
  allowed_by: string;
  reason: string;
}

export async function listAllowlist(
  id: Pick<Identity, "org" | "namespace">,
  opts: { limit?: number } & RequestOpts = {},
): Promise<{ rows: AllowlistRow[] }> {
  return post(
    "/v1/conflicts.allowlist_list",
    {
      org: id.org,
      namespace: id.namespace,
      limit: opts.limit || 100,
    },
    opts.signal,
  );
}

export async function removeAllowlist(
  id: Pick<Identity, "org" | "namespace">,
  memory_key_a: string,
  memory_key_b: string,
  opts: { reason?: string; removed_by?: string } = {},
): Promise<{ removed: boolean }> {
  return post("/v1/conflicts.allowlist_remove", {
    org: id.org,
    namespace: id.namespace,
    memory_key_a,
    memory_key_b,
    reason: opts.reason ?? "",
    removed_by: opts.removed_by ?? "",
  });
}

export async function resolveConflict(
  id: Pick<Identity, "org" | "namespace">,
  memory_id: string,
  decision: ResolveDecision,
  opts: { peer_id?: string; reason?: string; resolved_by?: string } = {},
): Promise<{
  resolved: boolean;
  affected_memory_ids: string[];
  /** The namespace's conflicted count after the writes; absent from a server a release
   *  behind, null when nothing was resolved. */
  total?: number | null;
}> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    memory_id,
    decision,
    reason: opts.reason ?? "",
    resolved_by: opts.resolved_by ?? "",
  };
  if (opts.peer_id) body["peer_id"] = opts.peer_id;
  return post("/v1/conflicts.resolve", body);
}

// ===== cost.budget_status =====

export interface BudgetStatus {
  namespace: string;
  spent_24h_usd: number;
  cap_usd: number;
  cap_source: "namespace" | "global" | "uncapped" | "org";
  over_cap: boolean;
  pct_of_cap: number | null;
  namespaces_with_spend?: number;
}

/** Current 24h LLM spend vs the effective daily cap for a namespace.
 *  Drives the Usage view's budget bar + the "extraction paused" warning. */
export async function getBudgetStatus(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts = {},
): Promise<BudgetStatus> {
  return post("/v1/cost.budget_status", { org: id.org, namespace: id.namespace }, opts.signal);
}

/** Org-wide 24h LLM spend rollup (all namespaces in the org). For
 *  admin visibility into "what's this tenant burning across the
 *  whole org" without per-namespace summation. */
export async function getOrgBudgetStatus(id: Pick<Identity, "org">, opts: RequestOpts = {}): Promise<BudgetStatus> {
  return post("/v1/cost.budget_status", { org: id.org }, opts.signal);
}

// ============================================================
// Self-service namespace membership
// ============================================================

export type Visibility = "private" | "public";

export interface SelfSignupResponse {
  token: string;
  namespace: string;
  // Personal namespace + the explicit one — what the user now owns.
  namespaces: string[];
  org: string;
  subject: string;
  expires_at: string | null;
}

// Password sign-in: classic email-or-namespace + password → JWT.
export interface LoginResponse {
  token: string;
  subject: string;
  org: string;
  namespaces: string[];
  expires_at: string | null;
}

export async function loginWithPassword(identifier: string, password: string): Promise<LoginResponse> {
  const r = await fetch("/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier, password }),
    signal: boundedSignal(),
  });
  if (!r.ok) throw await apiErrorFromResponse(r);
  return readJson(r);
}

// Admin claim-release: revoke the grants on a namespace whose owner lost their token and
// deactivate it. The name stays reserved.
export interface ClaimReleaseResponse {
  released: boolean;
  namespace: string;
  previous_owner: string;
  grants_revoked: number;
}

export async function claimRelease(org: string, namespace: string, reason: string = ""): Promise<ClaimReleaseResponse> {
  return post<ClaimReleaseResponse>("/v1/admin/claim_release", { org, namespace, reason });
}

/** Mint a JWT for a chosen namespace. Returns 404 if LOOM_SELF_SIGNUP=off. */
export async function selfSignup(
  namespace: string,
  opts: { org?: string; ttl_days?: number; email?: string; password?: string } = {},
): Promise<SelfSignupResponse | null> {
  const body: Record<string, unknown> = { namespace };
  if (opts.org) body["org"] = opts.org;
  if (opts.ttl_days) body["ttl_days"] = opts.ttl_days;
  if (opts.email) body["email"] = opts.email;
  if (opts.password) body["password"] = opts.password;
  const r = await fetch("/v1/auth/self-signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: boundedSignal(),
  });
  if (r.status === 404) return null; // self-signup disabled
  if (!r.ok) throw await apiErrorFromResponse(r);
  return readJson(r);
}

export interface MyNamespaceEntry {
  namespace: string;
  role: "viewer" | "writer" | "admin" | "jwt";
  status?: "active" | "pending";
  pending_role?: "viewer" | "writer" | "admin" | null;
  pending_since?: string | null;
  pending_admins?: string[];
  visibility: Visibility;
  /** A client token reads this through its creator's grant; the role is the creator's. Never true for a person's own rows. */
  inherited?: boolean;
  /** "memory" (default), "code" — a code-plane repo index (the Code
   *  section), "qa" — a tracker read live, or "bench". Code and QA
   *  namespaces are reference data, never the landing default, but a claim on one is
   *  pickable: Patterns mode lives there. */
  kind?: "memory" | "code" | "tracker" | "qa" | "bench";
  description: string;
  joined_at: string | null;
  is_new: boolean;
}

/** POST /v1/namespace.mark_seen: the caller picked `namespace` in the picker, so list_mine
 *  stops calling it new. */
export async function markNamespaceSeen(org: string, namespace: string): Promise<void> {
  return post("/v1/namespace.mark_seen", { org, namespace });
}

export async function listMine(org: string, opts: RequestOpts = {}): Promise<MyNamespaceEntry[]> {
  const r = await post<{ rows: MyNamespaceEntry[] }>("/v1/namespace.list_mine", { org }, opts.signal);
  return r.rows;
}

export interface MemoryProvenanceLink {
  direction: string; // "out" = this → other; "in" = other → this
  memory_key: string;
  relation: string;
  label: string;
}

export interface MemoryProvenance {
  found: boolean;
  source_trace: string;
  source_kind: string; // conversation | extracted | observed | consolidated | app | manual | other
  source_label: string;
  thread_id: string;
  thread_exists: boolean; // is the source conversation transcript still retained?
  thread_title: string;
  agent: string;
  session_id: string;
  run_id: string;
  written_at: string;
  source_resource_id: string; // primary cited source (file/doc/URL), if stamped
  links: MemoryProvenanceLink[]; // authored derived_from/etc. edges (memory.link)
}

// Resolve where a memory came from (source_trace + retained-transcript check).
export async function getProvenance(id: Identity, memoryKey: string): Promise<MemoryProvenance> {
  return post<MemoryProvenance>("/v1/memory.provenance", { ...id, memory_key: memoryKey });
}

export interface CopyToResult {
  copied: boolean;
  target_memory_id: string;
  target_namespace: string;
  target_key: string;
}

// Self-service copy of a memory into another namespace (memory.copy_to).
// Non-destructive: the source stays put; the target row is re-embedded under
// the target namespace's own embedder. Needs WRITER on the target namespace.
export async function copyMemoryTo(
  source: Identity,
  memoryKey: string,
  toNamespace: string,
  opts: { targetKey?: string; rationale?: string } = {},
): Promise<CopyToResult> {
  return post<CopyToResult>("/v1/memory.copy_to", {
    from_org: source.org,
    from_namespace: source.namespace,
    memory_key: memoryKey,
    // A row opened under its user and session is copied as that exact row; otherwise the
    // newest row of the key, whoever wrote it, as the Memories list shows it.
    ...(source.user_id || source.session_id
      ? { from_agent: source.agent, from_user_id: source.user_id ?? "-", from_session_id: source.session_id ?? "-" }
      : { from_agent: "*" }),
    to_namespace: toNamespace,
    target_key: opts.targetKey ?? null,
    rationale: opts.rationale ?? "",
  });
}

export interface SharingAgentRow {
  agent: string;
  writes: number;
  recalls: number;
  cross_namespace_recalls: number;
}
/** One agent's activity inside one namespace: the rows the Flow page filters to the namespace it is showing. */
export interface SharingAgentNamespaceRow extends SharingAgentRow {
  namespace: string;
}
export interface SharingNamespaceRow {
  namespace: string;
  role: string;
  visibility: string;
  member_count: number;
  writes_7d: number;
  recalls_7d: number;
}
export interface SharingSummary {
  subject: string;
  org: string;
  window_days: number;
  totals: {
    writes: number;
    recalls: number;
    cross_namespace_recalls: number;
    namespaces: number;
    agents: number;
  };
  agents: SharingAgentRow[];
  /** Per agent × namespace. */
  agent_namespaces: SharingAgentNamespaceRow[];
  namespaces: SharingNamespaceRow[];
}

export async function getMySharingSummary(opts: RequestOpts = {}): Promise<SharingSummary> {
  return getJson<SharingSummary>("/v1/me/sharing-summary", opts);
}

export interface TokenEntry {
  name: string;
  sa_subject: string;
  namespaces: string[];
  roles: string[];
  ttl_days: number;
  created_by: string;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_by: string;
  /** Max ts seen on agent_events for this SA's subject. Null = never
   *  used since creation. */
  last_used_at?: string | null;
  /** Made by you, under any spelling of yours (not another person's token that reaches a
   *  namespace you administer). Absent from the previous build's answer. */
  yours?: boolean;
  /** Authoritative per-namespace role map for scoped tokens
   *  (namespace -> "viewer"|"writer"|"admin"), each capped to the minter's
   *  grant. Empty for unscoped tokens (fall back to `roles`). */
  ns_roles?: Record<string, string>;
  /** Where a list token saves, as minted; "" for a `*` token, one with no default target,
   *  or one minted before tokens recorded it. Absent from the previous build's answer. */
  saves_to?: string;
}

/** One namespace of a new token: a name, `*` (every namespace you belong to, now and
 *  later), or a name with the role the token gets there. */
export type TokenNamespace = string | { namespace: string; role: "viewer" | "writer" | "admin" };

export interface TokenCreated {
  /** Shown in this answer only. */
  token: string;
  name: string;
  sa_subject: string;
  org: string;
  /** The claim as minted: `*` and what is named beside it, or the list it reads. */
  namespaces: string[];
  ns_roles: Record<string, string>;
  /** The one role of a token without `ns_roles`; [] for a list with a role each. */
  roles: string[];
  reads_everything: boolean;
  /** Where calls that name no namespace save; "" none; null not readable now. */
  saves_to: string | null;
  created_at: string;
  expires_at: string | null;
  /** Restricted namespaces the token reads with a grant of its own. */
  restricted_namespaces: string[];
  mcp_url: string;
}

export async function listTokens(): Promise<TokenEntry[]> {
  return (await getJson<{ rows: TokenEntry[] }>("/v1/me/service_accounts")).rows;
}

export async function createToken(opts: {
  /** Blank: the server names it `token-<day>-<hex>`. */
  name?: string;
  /** `*` and what is named beside it (a name before `*` is where it saves), or a list in
   *  read priority. */
  namespaces: TokenNamespace[];
  ttl_days: number | null;
  /** A list's save target, one of its memory namespaces it writes; not sent with `*`. */
  saves_to?: string;
}): Promise<TokenCreated> {
  const r = await post<Partial<TokenCreated> & Pick<TokenCreated, "token" | "name" | "namespaces">>(
    "/v1/me/service_accounts.create",
    opts,
  );
  // A replica of the previous build answers a list mint without the fields it added
  // (docs/architecture/rolling-upgrades.md, rule 35): the token is still shown.
  return {
    sa_subject: "",
    org: "",
    ns_roles: {},
    roles: [],
    created_at: "",
    expires_at: null,
    restricted_namespaces: [],
    ...r,
    reads_everything: r.reads_everything ?? r.namespaces.includes("*"),
    saves_to: r.saves_to ?? null,
    mcp_url: r.mcp_url ?? `${window.location.origin}/mcp`,
  };
}

export async function revokeToken(name: string): Promise<void> {
  await post("/v1/me/service_accounts.revoke", { name });
}

export interface BrowseEntry {
  namespace: string;
  kind?: "memory" | "code" | "tracker" | "qa" | "bench";
  description: string;
  owner_email: string;
  member_count: number;
  requires_approval?: boolean;
  created_at: string;
  /** Resolved join policy, discoverability, the caller's standing, and for a code row the repository and branch it indexes. */
  join_policy?: "open" | "approval" | "closed";
  visibility?: Visibility;
  my_status?: "none" | "pending" | "member";
  my_role?: string;
  repo?: string;
  branch?: string;
  /** The namespace's guide, on a public row or one the caller belongs to: its current revision (0 = none) and one-line summary. */
  guide_revision?: number;
  guide_summary?: string;
}

export async function browseNamespaces(
  org: string,
  opts: { query?: string; limit?: number; kinds?: string[]; includeClosed?: boolean } & RequestOpts = {},
): Promise<BrowseEntry[]> {
  const body: Record<string, unknown> = { org };
  if (opts.query) body["query"] = opts.query;
  if (opts.limit) body["limit"] = opts.limit;
  if (opts.kinds && opts.kinds.length) body["kinds"] = opts.kinds;
  if (opts.includeClosed) body["include_closed"] = true;
  const r = await post<{ rows: BrowseEntry[] }>("/v1/namespace.browse", body, opts.signal);
  return r.rows;
}

/** One saved revision of a namespace's guide: who saved it, when and why (`namespace.guide_history`). */
export interface GuideRevision {
  revision: number;
  note: string;
  updated_by: string;
  updated_at: string;
}

export const GUIDE_HISTORY_LIMIT = 50;

/** The guide's revisions, newest first, the current one among them. */
export async function guideHistory(
  org: string,
  namespace: string,
  limit: number,
  opts: RequestOpts = {},
): Promise<GuideRevision[]> {
  const r = await post<{ revisions: GuideRevision[] }>(
    "/v1/namespace.guide_history",
    { org, namespace, limit },
    opts.signal,
  );
  return r.revisions;
}

/** A namespace's guide (`namespace.guide_get`); `revision` 0 when it never had one. */
export interface NamespaceGuide {
  found: boolean;
  revision: number;
  content: string;
  summary: string;
  note: string;
  updated_by: string;
  updated_at: string | null;
}

export interface GuideSaveResult {
  written: boolean;
  /** The guide moved past `expected_revision`: nothing was written, `current` is what is there now. */
  conflict: boolean;
  revision: number;
  current: NamespaceGuide | null;
}

export async function getNamespaceGuide(
  org: string,
  namespace: string,
  opts: RequestOpts = {},
): Promise<NamespaceGuide> {
  return post<NamespaceGuide>("/v1/namespace.guide_get", { org, namespace }, opts.signal);
}

export async function saveNamespaceGuide(
  org: string,
  namespace: string,
  edit: { content: string; summary?: string; note: string; expected_revision: number },
): Promise<GuideSaveResult> {
  return post<GuideSaveResult>("/v1/namespace.guide_set", { org, namespace, ...edit });
}

export interface JoinResponse {
  joined: boolean;
  namespace: string;
  role: "viewer" | "writer" | "admin";
  status?: "active" | "pending";
  /** Set when the request landed pending — what they're queued for. */
  requested_role?: "viewer" | "writer" | "admin" | null;
  /** Subjects who can approve the pending request. */
  pending_admins?: string[];
  joined_at: string;
}

export async function joinNamespace(
  org: string,
  namespace: string,
  opts: { role?: "viewer" | "writer" | "admin" } = {},
): Promise<JoinResponse> {
  const body: Record<string, unknown> = { org, namespace };
  if (opts.role) body.role = opts.role;
  return post<JoinResponse>("/v1/namespace.join", body);
}

/** Withdraw the caller's own pending elevation request. Keeps their
 *  base grant (typically viewer) intact; only clears pending_role. */
export async function cancelNamespaceRequest(
  org: string,
  namespace: string,
): Promise<{ cancelled: boolean; namespace: string; cleared_role: string | null; cancelled_at: string | null }> {
  return post("/v1/namespace.cancel_request", { org, namespace });
}

/** Give up the caller's own membership of a namespace (their grant is
 *  revoked; nothing else changes). Used by the Code view's Leave. */
export async function leaveNamespace(org: string, namespace: string): Promise<{ left: boolean; namespace: string }> {
  return post("/v1/namespace.leave", { org, namespace });
}

export type CodeId = { org: string; namespace: string };
// Every reader of the shared repository list — the Repositories page, its cards, the
// bell, Setup — reads these; two keys (scip_py_lag, has_maintainer) nothing reads stay on
// the server. `tier_health` is not named: the server always sends it, and a replica of
// the build before it, during a rolling upgrade, refuses a field it does not have.
const CODE_REPOS_FIELDS = [
  "org",
  "namespace",
  "repo",
  "branch",
  "git_head",
  "clone_head",
  "fast_tier_lag",
  "scip_lag",
  "scip_producer",
  "indexed_at",
  "last_refresh_at",
  "counts",
  "readable",
  "join_policy",
  "my_status",
  "my_role",
  "source",
  "classification",
  "unreadable_reason",
  "stage",
  "stage_detail",
  "chunks_total",
  "chunks_embedded",
  "embedding_coverage",
  "searchable",
  "docs_namespace",
  "bound_trackers",
];
export async function listCodeRepos(org: string, opts: RequestOpts = {}): Promise<CodeRepoSummary[]> {
  const r = await post<{ repos: CodeRepoSummary[] }>(
    "/v1/code.repos",
    {
      org,
      fields: CODE_REPOS_FIELDS,
    },
    opts.signal,
  );
  return r.repos;
}

export async function codeIndexStatus(id: CodeId, opts: RequestOpts = {}): Promise<CodeIndexStatus> {
  return post<CodeIndexStatus>("/v1/code.index_status", id, opts.signal);
}

export interface TaxonomyEntry {
  value: string;
  count: number;
}
export interface TaxonomyCommunity {
  community_id: number;
  label: string;
  size: number;
  top_entities: string[];
}
export interface TaxonomyResponse {
  tags: TaxonomyEntry[];
  entities: TaxonomyEntry[];
  memory_types: TaxonomyEntry[];
  memory_count: number;
  communities: TaxonomyCommunity[];
  communities_computed_at: string;
}

export async function getTaxonomy(
  id: { org: string; namespace: string },
  opts: { top_n_tags?: number; top_n_entities?: number; top_n_communities?: number } & RequestOpts = {},
): Promise<TaxonomyResponse> {
  return post<TaxonomyResponse>(
    "/v1/namespace.taxonomy",
    {
      org: id.org,
      namespace: id.namespace,
      top_n_tags: opts.top_n_tags ?? 20,
      top_n_entities: opts.top_n_entities ?? 40,
      top_n_communities: opts.top_n_communities ?? 12,
    },
    opts.signal,
  );
}

// The planes the shell does not call live in api/*.ts; the barrel re-exports them for every importer.
export * from "./api/knowledge";
export * from "./api/conversations";
export * from "./api/admin";
export * from "./api/review";
// The audit-log read the sidebar badge calls, kept apart from admin.ts so the shell carries only it.
export * from "./api/audit";
export * from "./api/autoResolve";
export * from "./api/collections";
export * from "./api/search";
export * from "./api/profile";
export * from "./api/code";
export * from "./api/runs";
export * from "./api/facts";
export * from "./api/tracker";
export * from "./api/assistant";
