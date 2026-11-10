// How the dashboard words a failure: one line that says what happened and what to do
// (docs/development/dashboard-design.md, errors). Kept out of lib/format.ts so the boot
// chunk does not carry it: the toast viewport loads it soon after the shell paints, and
// toastError falls back to its context alone if it cannot.
import { ApiError, isLostConnection, LOST_CONNECTION, PUBLICATION_PENDING, TIMED_OUT } from "./errors";
import { errorRequestId } from "./format";

/** Sentences for the server's error codes (`server/core/errors.py` and the
 *  `HTTPException` details in `server/main.py`), kept only where the code says more than
 *  its category or status would. The server's own message rides along after the sentence
 *  (it states the cause): a sentence ending in ":" introduces it; one ending in "."
 *  precedes it. No sentence says to try again: `retryTail` owns that, from the body and,
 *  for the one code the dashboard makes itself, from the code. */
export const ERROR_CODE_PREFIXES: Record<string, string> = {
  AUTH_INVALID: "Sign-in rejected:",
  SIGNED_IN_ELSEWHERE: "You signed in afresh in another tab. Nothing was lost.",
  IDEMPOTENT_IN_PROGRESS: "This request is still running:",
  // Made by the dashboard (api.ts readJson): an ok answer whose body is not JSON.
  ANSWER_UNREADABLE: "Loom's answer could not be read; a proxy in front of it may have answered instead.",
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
  IDEMPOTENT_CONFLICT: "This request key was already used for a different request:",
  BLACKLISTED: "Blocked by a privacy filter:",
  BLOB_GONE: "The original file is no longer stored, so it can't be downloaded or extracted.",
  RATE_LIMITED: "Too many requests.",
  OVERLOADED: "Loom is busy right now.",
  BACKEND_TIMEOUT: "Loom took too long to answer.",
  CODED_WIDTH_MISMATCH: "The embedding model's width doesn't match the vector tables — see Settings › Models & keys.",
  SELF_SIGNUP_DISABLED: "Sign-up is off on this instance. Ask your operator.",
  SCHEMA_PENDING: "Loom's database is not upgraded for this yet:",
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

/** When the body carried no code we know, the HTTP status still says what
 *  kind of failure it was, without naming a cause. */
function sentenceForStatus(status: number): string {
  if (status === 401) return "Sign-in rejected:";
  if (status === 403) return "Not allowed:";
  if (status === 404) return "Not found:";
  if (status === 400 || status === 422) return "Not accepted:";
  if (status === 413) return "Too large:";
  if (status === 429) return "Too many requests.";
  if (status === 504) return "Loom didn't answer in time:";
  if (status === 502 || status === 503) return "Loom didn't answer:";
  if (status >= 500) return "Loom hit a server error:";
  return "Couldn't do that:";
}

/** What to do next, from the body alone: whether resending can work and when. Silent when
 *  the server's own words already say it. */
function retryTail(err: ApiError, read: boolean): string {
  // Made by the dashboard (api.ts) from a 401 that came before any handler ran: no body.
  if (err.code === "SIGNED_IN_ELSEWHERE") return "It is safe to send it once more.";
  const mayHaveLanded = read ? "Try again in a moment." : "It may have been saved; check before trying again.";
  // Made by the dashboard (api.ts readJson): the answer was ok, only its body unreadable.
  if (err.code === "ANSWER_UNREADABLE") return mayHaveLanded;
  const category = typeof err.detail.category === "string" ? err.detail.category : "";
  const retryable = err.detail.retryable;
  // A 5xx not marked retryable may follow a write the call made (`handler_failure_detail`;
  // a handler that raised partway answers INTERNAL_ERROR; a proxy's 502 or 504 says
  // nothing). It wins over a message that says to retry: the message was written for the
  // error, the mark for this call. A caller that only read (a dry run too) says so.
  if (err.status >= 500 && retryable !== true && category !== "configuration") {
    if (!read) return mayHaveLanded;
    if (retryable === false && (category === "capacity" || category === "dependency")) return mayHaveLanded;
  }
  if (/\b(retry\w*|again|resend\w*)\b/i.test(err.serverMessage)) return "";
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
  // After a full stop the server's words start a sentence of their own.
  return `${friendly} ${friendly.endsWith(".") ? asSentence(msg) : msg}`;
}

/** A server message set after a full stop: capitalised and closed. */
function asSentence(msg: string): string {
  const t = msg.trim();
  const closed = /[.!?]$/.test(t) ? t : `${t}.`;
  // A message that opens with an identifier (`if_match_sha256 …`, `memory.set …`) keeps it.
  return /^[a-z]+(\s|$)/.test(closed) ? closed.charAt(0).toUpperCase() + closed.slice(1) : closed;
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
 *  errors map too; a dashboard bug reads as one; any other value is shown as its text. */
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
  if (err.code === "PUBLICATION_PENDING") return PUBLICATION_PENDING;
  const lead = leadFor(err);
  let text: string;
  if (err.serverMessage && bare) {
    // The caller's lead-in replaces a generic one; a code's sentence stays, since it
    // carries the next step ("Your role doesn't allow this.").
    const own = codeSentence(err);
    text = own ? `${own.replace(/:$/, ".")} ${asSentence(err.serverMessage)}` : err.serverMessage;
  } else if (err.serverMessage) text = joinSentence(lead, err.serverMessage);
  // With no message the code is all the server said: kept for support, never the sentence.
  // A generic lead-in says little, so the code goes with it (or alone, after a caller's own
  // lead-in); a code's own sentence already says it all.
  else if (err.code && !codeSentence(err))
    text = bare ? `error code ${err.code}.` : `${lead.replace(/[:.]$/, "")} (${err.code}).`;
  else text = joinSentence(lead, "");
  return withTail(text, retryTail(err, read));
}

/** An inline line's reason ("Couldn't load …:"): the bare words and, when the failure
 *  carried one, the request id, since the sentence may ask the reader to send it. A read's
 *  by default; a write's line passes `read: false`, so a lost connection keeps its warning. */
export function errorLine(err: unknown, opts: { read?: boolean } = {}): string {
  const rid = errorRequestId(err);
  const text = humanizeError(err, { bare: true, read: opts.read ?? true });
  return rid ? `${/[.!?]$/.test(text) ? text : `${text}.`} Request id ${rid}.` : text;
}
