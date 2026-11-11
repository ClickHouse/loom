// How the dashboard words a failure: one line that says what happened and what to do
// (docs/development/dashboard-design.md, errors). Kept out of lib/format.ts so the boot
// chunk does not carry it: the toast viewport loads it soon after the shell paints, and
// toastError falls back to its context alone if it cannot. lib/staleChunk.ts does not reload
// when this chunk fails to load, where the browser names the file (Chrome, Firefox).
// The order a line is built in:
//   1. the code's own sentence, where it says more than its category or status;
//   2. the body's category, unless `input` (the server's word for any other 4xx);
//   3. the HTTP status;
// then the server's words, then one tail from the body: when it can go again, that it may
// have saved (a write; `read` drops it), or that only a change to the setup helps.
import {
  ApiError,
  isLostConnection,
  isPublicationPending,
  LOST_CONNECTION,
  MAY_HAVE_SAVED,
  mayHaveSaved,
  PUBLICATION_PENDING,
  TIMED_OUT,
} from "./errors";
import { errorRequestId } from "./format";
import { toastError, toastWithId, type ToastOpts } from "../components/Toast";

/** Sentences for the server's error codes (`server/core/errors.py` and the
 *  `HTTPException` details in `server/main.py`), kept only where the code says more than
 *  its category or status would. The server's own message rides along after the sentence
 *  (it states the cause): a sentence ending in ":" introduces it; one ending in "."
 *  precedes it. No sentence says to try again: `retryTail` owns that, from the body and,
 *  for the codes the dashboard makes itself (SIGNED_IN_ELSEWHERE, ANSWER_UNREADABLE), from
 *  the code; STREAM_ENDED, the third, takes the body's rules. */
export const ERROR_CODE_PREFIXES: Record<string, string> = {
  AUTH_INVALID: "Sign-in rejected:",
  SIGNED_IN_ELSEWHERE: "You signed in afresh in another tab. Nothing was lost.",
  IDEMPOTENT_IN_PROGRESS: "Not done yet:",
  // Made by the dashboard (api.ts readJson): an ok answer whose body is not JSON.
  ANSWER_UNREADABLE: "Loom's answer could not be read; a proxy in front of it may have answered instead.",
  AUTH_INSUFFICIENT: "Your role doesn't allow this:",
  IDENTITY_INCOMPLETE: "This token is scoped, so the request has to name the namespace:",
  POLICY_FORBIDDEN: "Blocked by a policy:",
  TOKEN_NAME_TAKEN: "Name taken:",
  COLLECTION_NAME_TAKEN: "Name taken:",
  NOT_FOUND: "Not found; it may have been deleted, or be somewhere you can't read:",
  RESOURCE_NOT_FOUND: "Not found; it may have been deleted, or be somewhere you can't read:",
  NAMESPACE_NOT_FOUND: "That namespace doesn't exist:",
  NAMESPACE_NOT_ACCESSIBLE: "You don't have access to that namespace:",
  CHAR_LIMIT_EXCEEDED: "Too long:",
  PAYLOAD_TOO_LARGE: "Too large:",
  UNSUPPORTED_FILE: "Loom can't read this file type:",
  IDEMPOTENT_CONFLICT: "This request key was already used for a different request:",
  BLACKLISTED: "Blocked by a privacy filter:",
  BLOB_GONE: "The original file is no longer stored, so it can't be downloaded or extracted:",
  RATE_LIMITED: "Over a limit for now:",
  OVERLOADED: "Loom is busy right now:",
  BACKEND_TIMEOUT: "Loom took too long to answer:",
  CODED_WIDTH_MISMATCH: "The embedding model's width doesn't match the vector tables (see Settings › Models & keys):",
  SELF_SIGNUP_DISABLED: "Sign-up is off on this instance; ask your operator:",
  SCHEMA_PENDING: "Loom's database is not upgraded for this yet:",
  ASSISTANT_UNAVAILABLE: "The assistant is not available:",
  ASSISTANT_UPSTREAM_FAILED: "The assistant could not finish this answer:",
  // Made by the dashboard (api/assistantChat.ts): the answer's stream stopped before its end.
  STREAM_ENDED: "The answer was cut off:",
  INTERNAL_ERROR: "Loom hit an internal error. If it keeps happening, tell your operator.",
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

/** Refusals whose check could not run answer with category `dependency` (server
 *  `unverified_class`): the caller may well be allowed, so no sentence about roles. */
const UNVERIFIED_CODES = new Set(["AUTH_INSUFFICIENT", "POLICY_FORBIDDEN", "NAMESPACE_NOT_ACCESSIBLE"]);

/** Whose move an error is (`category` on every coded body, server `classified`). `input`
 *  is left out: the server gives it to every 4xx it does not class otherwise (a missing
 *  row, a name clash), so the status says more. */
const CATEGORY_SENTENCES: Record<string, string> = {
  auth: "Not allowed:",
  capacity: "Loom is busy:",
  dependency: "A service Loom relies on failed:",
  configuration: "This needs a change to Loom's setup:",
  internal: "Loom hit an internal error:",
};

/** The lead-in for a status that says nothing about why. */
const UNKNOWN_STATUS = "Couldn't do that:";

/** When the body carried no code we know, the HTTP status still says what
 *  kind of failure it was, without naming a cause. */
function sentenceForStatus(status: number): string {
  if (status === 401) return "Sign-in rejected:";
  if (status === 403) return "Not allowed:";
  if (status === 404) return "Not found:";
  if (status === 400 || status === 422) return "Not accepted:";
  if (status === 413) return "Too large:";
  if (status === 429) return "Over a limit for now:";
  if (status === 504) return "Loom didn't answer in time:";
  if (status === 502 || status === 503) return "Loom didn't answer:";
  if (status >= 500) return "Loom hit a server error:";
  return UNKNOWN_STATUS;
}

/** Whose move an error is, from its body (`classified` on the server); "" without one. */
function categoryOf(err: ApiError): string {
  return typeof err.detail.category === "string" ? err.detail.category : "";
}

/** What to do next, from the body alone: whether resending can work and when. Silent when
 *  the server's own words already say it. */
function retryTail(err: ApiError, read: boolean): string {
  // Made by the dashboard (api.ts) from a 401 that came before any handler ran: no body.
  if (err.code === "SIGNED_IN_ELSEWHERE") return "It is safe to send it once more.";
  const category = categoryOf(err);
  const retryable = err.detail.retryable;
  // A write the server marks unsafe to resend says so, even when its message says to retry;
  // a read is told it can go again, unless the server's words already say so.
  if (mayHaveSaved(err) && !read) return MAY_HAVE_SAVED;
  if (/\b(retry\w*|again|resend\w*)\b/i.test(err.serverMessage)) return "";
  if (
    mayHaveSaved(err) &&
    (err.code === "ANSWER_UNREADABLE" ||
      (retryable === false && (category === "capacity" || category === "dependency")))
  )
    return "Try again in a moment.";
  if (category === "configuration") return "Trying again won't help until that's changed.";
  const wait = Number(err.detail.retry_after_s ?? err.retryAfter);
  if (retryable === true) return wait > 0 ? `Try again in ${Math.ceil(wait)} s.` : "Try again in a moment.";
  // A body with no class (a proxy's answer) may still carry the header's wait.
  if (retryable === undefined && wait > 0) return `Try again in ${Math.ceil(wait)} s.`;
  return "";
}

function joinSentence(friendly: string, message: string): string {
  const msg = message.trim();
  if (!msg) return friendly.endsWith(":") ? friendly.slice(0, -1) + "." : friendly;
  // The server's words close the line, whatever introduced them.
  return `${friendly} ${asSentence(msg)}`;
}

/** A code's sentence and the server's words after a caller's own "…:": a dash where the
 *  sentence would end in a second colon. */
function bareJoin(sentence: string, message: string): string {
  const msg = asSentence(message);
  return sentence.endsWith(":") ? `${sentence.slice(0, -1)} — ${msg}` : `${sentence} ${msg}`;
}

/** A server message closed with a full stop, and otherwise left as written, since its first
 *  word may be a name (a lowercase namespace, a subject) that a capital would change. */
function asSentence(msg: string): string {
  const t = msg.trim();
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

function withTail(text: string, tail: string): string {
  if (!tail) return text;
  return `${/[.!?]$/.test(text) ? text : `${text}.`} ${tail}`;
}

/** The same, for a caller that only read (`read`): nothing can have committed. */
const LOST_CONNECTION_READ = "Lost connection to Loom.";
const TIMED_OUT_READ = "Loom didn't answer in time.";

/** Shown for anything thrown that is not a plain `Error`: the dashboard words its own
 *  refusals as plain Errors, while a TypeError, an AbortError or a library's own class
 *  carries text meant for a developer. */
const DASHBOARD_FAULT = "Something went wrong in the dashboard. Reload the page.";

export interface HumanizeOptions {
  /** Leave out the lead-in sentence: the caller supplies its own ("Couldn't delete x:"),
   *  so the line does not read "Couldn't delete x: Couldn't do that: …". */
  bare?: boolean;
  /** The call only read, so a lost connection cannot have left a write behind. */
  read?: boolean;
}

/** One readable line for any thrown value. An `ApiError` maps by server code, then by its
 *  category, then by HTTP status, and says whether trying again can help — the raw
 *  response body never reaches the screen. Plain `CODE: detail` strings and bare network
 *  errors map too; a plain Error or a SyntaxError shows its own words; anything else reads as
 *  the dashboard's own fault. */
export function humanizeError(err: unknown, opts: HumanizeOptions = {}): string {
  if (!err) return DASHBOARD_FAULT;
  if (err instanceof ApiError) return humanizeApiError(err, opts.bare === true, opts.read === true);
  const raw = err instanceof Error ? err.message : String(err);
  // Raw network errors (connection drop, fetch aborted, etc.) come
  // back as bare strings like "Failed to fetch" / "fetch failed" /
  // "Load failed" / "NetworkError when attempting…". Distinguish "we
  // never reached the server" from "the server said no" — and warn that
  // the server may have committed the work even though we lost the response.
  if (isLostConnection(raw)) {
    return opts.read ? LOST_CONNECTION_READ : LOST_CONNECTION;
  }
  // A DOMException is not an Error in every engine, so the name is read off the object;
  // a thrown object with no name is not something the dashboard worded.
  const name = typeof err === "string" ? "Error" : typeof err === "object" && "name" in err ? String(err.name) : "";
  // Every call is bounded (`AbortSignal.timeout`, api.ts): a slow server, not a bug here.
  if (name === "TimeoutError") return opts.read ? TIMED_OUT_READ : TIMED_OUT;
  if (name === "AbortError") return "Cancelled.";
  // A plain Error carries the dashboard's own words; a SyntaxError, the parser's on text
  // or a file the person supplied (a policy's JSON, a snapshot file). A server answer that
  // is not JSON is an ApiError instead (api.ts readJson), so it never lands here.
  if (!raw || (name !== "Error" && name !== "SyntaxError")) return DASHBOARD_FAULT;
  const m = /^([A-Z][A-Z_]+):?\s*(.*)$/s.exec(raw);
  if (!m) return raw;
  const friendly = ERROR_CODE_PREFIXES[m[1]];
  if (!friendly) return raw;
  const detail = m[2] ?? "";
  return opts.bare && detail.trim() ? bareJoin(friendly, detail) : joinSentence(friendly, detail);
}

/** The server's words (server/auth/bearer.py) for a call sent after the session ended in
 *  another tab took the token; a refusal of a token the call carried is `sessionEnded`. */
const SESSION_GONE = /missing bearer token/;

/** Sentences `codeSentence` picks over a code's own, by what the body says: held to the
 *  same rules as the table (errorWords.test.ts). */
export const CONDITIONAL_SENTENCES = {
  token: "This token can't do this:",
  unverified: "Loom couldn't check your access just now:",
};

/** Refusals about the token itself, not the caller's role (the assistant's, in
 *  `server/assistant/app/bridge.py`; docs/reference/errors.md). */
const TOKEN_REASONS = new Set(["org_wide", "narrowed", "token_expires_soon"]);

/** The code's own sentence, when it says more than its category or status. */
function codeSentence(err: ApiError): string | undefined {
  const category = categoryOf(err);
  const reason = err.detail.reason;
  if (err.code === "AUTH_INSUFFICIENT" && typeof reason === "string" && TOKEN_REASONS.has(reason))
    return CONDITIONAL_SENTENCES.token;
  if (UNVERIFIED_CODES.has(err.code) && category === "dependency") return CONDITIONAL_SENTENCES.unverified;
  return ERROR_CODE_PREFIXES[err.code];
}

function humanizeApiError(err: ApiError, bare: boolean, read: boolean): string {
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
  if (isPublicationPending(err)) return PUBLICATION_PENDING;
  // The server's words for it carry only the request id: the line says it once, in words,
  // wherever it is shown; a caller that also prints ids beside a line skips one it holds.
  if (err.code === "INTERNAL_ERROR") {
    // The header's id, else the one the server's words carry ("…; request_id=<id>").
    const rid = err.requestId || /request_id=([\w-]+)/.exec(err.serverMessage)?.[1] || "";
    const id = rid ? ` Request id ${rid}.` : "";
    return withTail(`${ERROR_CODE_PREFIXES.INTERNAL_ERROR}${id}`, retryTail(err, read));
  }
  const own = codeSentence(err);
  const lead = own ?? CATEGORY_SENTENCES[categoryOf(err)] ?? sentenceForStatus(err.status);
  let text: string;
  if (err.serverMessage && bare) {
    // The caller's lead-in replaces a generic one; a code's sentence stays.
    text = own ? bareJoin(own, err.serverMessage) : asSentence(err.serverMessage);
  } else if (err.serverMessage) text = joinSentence(lead, err.serverMessage);
  // With no message the code is all the server said: kept for support, never the sentence.
  // A generic lead-in says little, so the code goes with it; after a caller's own lead-in a
  // category's words still say what happened, a status's do not. A code's own sentence
  // already says it all.
  else if (err.code && !own)
    text =
      bare && !CATEGORY_SENTENCES[categoryOf(err)]
        ? `error code ${err.code}.`
        : `${lead.replace(/[:.]$/, "")} (${err.code}).`;
  // Nothing said and a status that means nothing: after a caller's own lead-in, only the
  // status is left to name ("Save failed: Loom answered with status 418.").
  else if (bare && !own && lead === UNKNOWN_STATUS) text = `Loom answered with status ${err.status}.`;
  else text = joinSentence(lead, "");
  return withTail(text, retryTail(err, read));
}

/** An inline line's reason ("Couldn't load …:"): the bare words and, when the failure
 *  carried one, the request id, since the sentence may ask the reader to send it. A read
 *  passes `read`; without it the line keeps the write warning, as `humanizeError` does. */
export function errorLine(err: unknown, opts: { read?: boolean } = {}): string {
  const rid = errorRequestId(err);
  const text = humanizeError(err, { bare: true, read: opts.read === true });
  return rid && !text.includes(rid) ? withTail(text, `Request id ${rid}.`) : text;
}

// A write's catch when the server answered `PUBLICATION_PENDING` (the write committed; only
// lists and search lag): here, beside the wording the pages that write already load, so the
// boot chunk carries none of it.
/** The line for a write that committed with only lists and search behind
 *  (`PUBLICATION_PENDING`), with its request id and an error's reading time. `msg` is a
 *  page's own line for it (what was done, what to do next); `opts` adds a key or an action. */
export function toastCommitted(
  err: unknown,
  msg: string = PUBLICATION_PENDING,
  opts: Pick<ToastOpts, "key" | "action"> = {},
): void {
  toastWithId(msg, "info", err, opts);
}

/** For a write's catch: toasts the failure as `toastError` does and says whether the write
 *  committed anyway (`PUBLICATION_PENDING`: only lists and search lag). On true the caller
 *  goes on as after a success, so the page shows the write as made and never invites a
 *  repeat; on false it handles the failure. */
export function committedAnyway(context: string, err: unknown): boolean {
  toastError(context, err);
  return isPublicationPending(err);
}
