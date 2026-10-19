/**
 * Sign in to a Loom server with no token to paste: `await login(server)`.
 * Mirrors the Python SDK's `loom.credentials` and shares its file, so a
 * session made by `loom login`, `loom.login()` or this `login()` is used by
 * all three.
 *
 * `login()` runs the OAuth device authorization grant (RFC 8628): it prints
 * a URL and a short code, you approve on any browser, and the access and
 * refresh tokens land in `~/.loom/credentials` (mode 0600), one entry per
 * profile. A client for that server given no `apiKey` and no `LOOM_API_KEY`
 * uses them, refreshing the hour-long access token as it goes. `logout()`
 * revokes the session on the server and forgets it here.
 *
 * The refresh token rotates on every use and the server ends the session
 * when a spent one comes back, so two processes must never refresh the same
 * profile at once: a refresh holds the directory lock
 * `~/.loom/.credentials.lock.d` (which the Python SDK takes too) and
 * re-reads the file first, so the second process finds the new token.
 *
 * Node-only: `node:fs`, `node:os`, `node:path`, `node:crypto` and
 * `node:child_process` are loaded on first use, so a browser bundle never
 * pulls them; in a browser a client never looks for a stored login.
 */

import { AuthError, BackendUnavailable, LoomError, NETWORK_ERROR_CODE } from "./errors.js";
import type { TokenProvider } from "./machine.js";
import { envVar } from "./runtime.js";

export const DEFAULT_SERVER = "http://localhost:7777";
export const CLIENT_ID = "loom-cli";
const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code";
// On every sign-in answer of a build that keeps sessions. Once a server has
// sent it, an answer without it came from a previous build during a rolling
// upgrade, which cannot know this session. A server never seen sending it (a
// proxy may strip it) has its refusals taken as they are.
const SESSIONS_HEADER = "Loom-Sessions";
// Refresh this long before the access token expires, so a call never leaves
// with a token that dies in flight.
const REFRESH_MARGIN_S = 60;
// Longer than a refresh can take on the server, which asks the identity
// provider first: giving up sooner turns a slow answer into a lost one.
const REFRESH_TIMEOUT_MS = 60_000;
const REQUEST_TIMEOUT_MS = 30_000;
/** A lock directory older than this was left by a process that died holding it. */
export const LOCK_STALE_MS = 2 * REFRESH_TIMEOUT_MS;
const LOCK_DIR = ".credentials.lock.d";

type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

export interface CredentialOptions {
  /** Injected fetch (tests). Default: globalThis.fetch. */
  fetch?: FetchFn;
}

export interface LoginOptions extends CredentialOptions {
  /** The profile to store the session under. Default `LOOM_PROFILE`, else `default`. */
  profile?: string;
  /** Open the approval page in a browser when stdout is a terminal. Default true. */
  openBrowser?: boolean;
  /** Where the code and the result are printed. Default: console.log. */
  echo?: (line: string) => void;
  /** Injected sleep between polls (tests). */
  sleep?: (ms: number) => Promise<void>;
}

/** What `whoami()` answers: the server's view of the token, with the profile and its server. */
export type WhoAmI = Record<string, unknown> & { profile: string; server: string };

interface Entry {
  server: string;
  session?: string;
  marked?: boolean;
  access_token: string;
  refresh_token?: string;
  expires_at: number;
}

interface CredentialsFile {
  profiles: Record<string, Entry>;
  [key: string]: unknown;
}

interface NodeModules {
  fs: typeof import("node:fs");
  os: typeof import("node:os");
  path: typeof import("node:path");
  crypto: typeof import("node:crypto");
  childProcess: typeof import("node:child_process");
}

type NodeProcess = {
  getBuiltinModule?: (id: string) => unknown;
  platform?: string;
  pid?: number;
  stdout?: { isTTY?: boolean };
};

function proc(): NodeProcess | undefined {
  return (globalThis as { process?: NodeProcess }).process;
}

/** The Node built-ins, loaded synchronously where the runtime can, else undefined. */
function nodeNow(): NodeModules | undefined {
  const get = proc()?.getBuiltinModule;
  if (typeof get !== "function") return undefined;
  const mods = {
    fs: get("node:fs"),
    os: get("node:os"),
    path: get("node:path"),
    crypto: get("node:crypto"),
    childProcess: get("node:child_process"),
  };
  return Object.values(mods).every(Boolean) ? (mods as NodeModules) : undefined;
}

let loaded: Promise<NodeModules | undefined> | undefined;

/** The Node built-ins, or undefined on a runtime without them (a browser, a worker). */
function node(): Promise<NodeModules | undefined> {
  loaded ??= (async () => {
    const now = nodeNow();
    if (now) return now;
    try {
      const [fs, os, path, crypto, childProcess] = await Promise.all(
        ["node:fs", "node:os", "node:path", "node:crypto", "node:child_process"].map(loadModule),
      );
      return { fs, os, path, crypto, childProcess } as NodeModules;
    } catch {
      return undefined;
    }
  })();
  return loaded;
}

async function nodeOrThrow(): Promise<NodeModules> {
  const mods = await node();
  if (!mods) throw new LoomError("signing in with a stored login needs Node (node:fs)");
  return mods;
}

// A computed specifier, so a bundler never pulls a Node built-in into a browser build.
function loadModule(name: string): Promise<unknown> {
  return import(/* webpackIgnore: true */ /* @vite-ignore */ name);
}

/** `~/.loom`, where the Python SDK keeps it: HOME (USERPROFILE on Windows), else the OS's answer. */
function loomDir(n: NodeModules): string {
  const home = envVar(proc()?.platform === "win32" ? "USERPROFILE" : "HOME") || n.os.homedir();
  return n.path.join(home, ".loom");
}

export function defaultProfile(): string {
  return envVar("LOOM_PROFILE")?.trim() || "default";
}

function read(n: NodeModules): CredentialsFile {
  let data: unknown;
  try {
    data = JSON.parse(n.fs.readFileSync(n.path.join(loomDir(n), "credentials"), "utf8"));
  } catch {
    return { profiles: {} };
  }
  const profiles = (data as { profiles?: unknown } | null)?.profiles;
  if (!data || typeof data !== "object" || Array.isArray(data) || !profiles || typeof profiles !== "object")
    return { profiles: {} };
  return data as CredentialsFile;
}

/** Replace the file atomically, readable by this user only. */
function write(n: NodeModules, data: CredentialsFile): void {
  const dir = loomDir(n);
  n.fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = n.path.join(dir, `.credentials-${n.crypto.randomBytes(6).toString("hex")}`);
  try {
    n.fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600, flag: "wx" });
    n.fs.renameSync(tmp, n.path.join(dir, "credentials"));
  } catch (e) {
    n.fs.rmSync(tmp, { force: true });
    throw e;
  }
}

function save(
  n: NodeModules,
  profile: string,
  server: string,
  tokens: Record<string, unknown>,
  session: string,
  marked: boolean,
): void {
  const data = read(n);
  data.profiles[profile] = {
    server,
    session,
    marked,
    access_token: String(tokens.access_token ?? ""),
    refresh_token: String(tokens.refresh_token ?? ""),
    expires_at: Math.floor(Date.now() / 1000) + Number(tokens.expires_in ?? 3600),
  };
  write(n, data);
}

function drop(n: NodeModules, profile: string): void {
  const data = read(n);
  if (data.profiles[profile] === undefined) return;
  delete data.profiles[profile];
  write(n, data);
}

// The lock between this process's callers; the lock directory is the one between processes.
let inProcess: Promise<unknown> = Promise.resolve();

/** Run `fn` holding the credentials lock: this process's, then the lock directory. */
function locked<T>(fn: (n: NodeModules) => Promise<T> | T): Promise<T> {
  const run = inProcess.then(async () => {
    const n = await nodeOrThrow();
    const release = await takeLockDir(n);
    try {
      return await fn(n);
    } finally {
      release();
    }
  });
  inProcess = run.catch(() => undefined);
  return run;
}

/**
 * Take `~/.loom/.credentials.lock.d` by creating it (mkdir is atomic on
 * every filesystem the file can live on), with an owner file naming the
 * holder. A directory older than LOCK_STALE_MS is broken. Returns the release.
 */
async function takeLockDir(n: NodeModules): Promise<() => void> {
  const dir = loomDir(n);
  n.fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const lock = n.path.join(dir, LOCK_DIR);
  const ownerFile = n.path.join(lock, "owner");
  const owner = JSON.stringify({ pid: proc()?.pid ?? 0, host: n.os.hostname(), time: Date.now() / 1000 });
  let wait = 10;
  for (;;) {
    try {
      n.fs.mkdirSync(lock, { mode: 0o700 });
      break;
    } catch (e) {
      if ((e as { code?: string }).code !== "EEXIST") throw e;
    }
    if (lockAgeMs(n, lock) > LOCK_STALE_MS) {
      n.fs.rmSync(lock, { recursive: true, force: true });
      continue;
    }
    await new Promise((resolve) => setTimeout(resolve, wait));
    wait = Math.min(wait * 2, 200);
  }
  try {
    n.fs.writeFileSync(ownerFile, owner, { mode: 0o600 });
  } catch (e) {
    n.fs.rmSync(lock, { recursive: true, force: true });
    throw e;
  }
  return () => {
    // Only our own: a lock broken as stale may be another process's by now.
    let held = "";
    try {
      held = n.fs.readFileSync(ownerFile, "utf8");
    } catch {
      return;
    }
    if (held === owner) n.fs.rmSync(lock, { recursive: true, force: true });
  };
}

function lockAgeMs(n: NodeModules, lock: string): number {
  try {
    return Date.now() - n.fs.statSync(lock).mtimeMs;
  } catch {
    return 0; // released meanwhile: take it on the next try
  }
}

/** (scheme, host, port) as a browser compares origins; undefined for no URL. */
function origin(url: string): string | undefined {
  try {
    return new URL(url).origin;
  } catch {
    return undefined;
  }
}

/** Whether two base URLs name the same server: the same origin and the same path, a trailing slash aside. */
export function sameServer(a: string, b: string): boolean {
  try {
    const [x, y] = [new URL(a), new URL(b)];
    return x.origin === y.origin && x.pathname.replace(/\/+$/, "") === y.pathname.replace(/\/+$/, "");
  } catch {
    return false;
  }
}

interface Answer {
  status: number;
  headers: Headers;
  text: string;
  url: string;
}

/** One request to the server; a failure to reach it is BackendUnavailable carrying the cause. */
async function exchange(
  f: FetchFn | undefined,
  url: string,
  init: { method?: string; form?: Record<string, string>; headers?: Record<string, string> },
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Answer> {
  const fetchFn = f ?? (globalThis.fetch as FetchFn | undefined);
  if (!fetchFn) throw new LoomError("no fetch available: pass `fetch` (Node 18 and later have one)");
  const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
  if (init.form) headers["Content-Type"] = "application/x-www-form-urlencoded";
  try {
    // Not followed, as the Python SDK's client does not: a code or token goes nowhere else.
    const res = await fetchFn(url, {
      method: init.method ?? (init.form ? "POST" : "GET"),
      headers,
      body: init.form ? new URLSearchParams(init.form).toString() : undefined,
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
    return { status: res.status, headers: res.headers, text: await res.text(), url };
  } catch (e) {
    const why = (e as { cause?: { code?: string } }).cause?.code ?? (e as Error).name ?? "error";
    throw new BackendUnavailable(`cannot reach ${origin(url) ?? url}: ${why}`, { cause: e, code: NETWORK_ERROR_CODE });
  }
}

const CONNECT_CODES = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "UND_ERR_CONNECT_TIMEOUT",
]);

/** Whether the request failed before reaching the server, so nothing it carried was spent. */
function neverConnected(e: unknown): boolean {
  const cause = (e as { cause?: { cause?: { code?: string } } }).cause;
  return CONNECT_CODES.has(cause?.cause?.code ?? "");
}

function parsed(r: Answer): unknown {
  try {
    return JSON.parse(r.text);
  } catch {
    return undefined;
  }
}

/** A 200's JSON object; anything else is the server's fault, said as one. */
function json(r: Answer): Record<string, unknown> {
  const body = parsed(r);
  if (body === undefined) throw new BackendUnavailable(`${r.url} answered something that is not JSON`);
  if (!body || typeof body !== "object" || Array.isArray(body))
    throw new BackendUnavailable(`${r.url} answered something that is not a JSON object`);
  return body as Record<string, unknown>;
}

function oauthField(r: Answer, field: "error" | "error_description"): string {
  const body = parsed(r);
  if (!body || typeof body !== "object") return "";
  const detail = "detail" in body ? (body as { detail: unknown }).detail : body;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return "";
  const value = (detail as Record<string, unknown>)[field];
  return value === undefined || value === null ? "" : String(value);
}

/** The typed SDK error for a refused sign-in request, as the Python SDK maps it. */
function failed(r: Answer, doing: string): LoomError {
  const error = oauthField(r, "error") || `HTTP ${r.status}`;
  const opts = { status: r.status, code: oauthField(r, "error") };
  if (r.status === 429 || r.status >= 500) return new BackendUnavailable(`${doing}: ${error}; retry`, opts);
  if (r.status === 401 || r.status === 403 || error === "invalid_grant")
    return new AuthError(`${doing}: ${error}; run \`loom login\` again`, opts);
  return new LoomError(`${doing}: ${error}`, opts);
}

type Endpoints = Record<"device_authorization_endpoint" | "token_endpoint" | "revocation_endpoint", string>;

/** The server's sign-in endpoints, each checked to be on the server itself. */
async function endpoints(f: FetchFn | undefined, server: string, signedIn = false): Promise<Endpoints> {
  const r = await exchange(f, `${server}/.well-known/oauth-authorization-server`, {});
  if (r.status !== 200 && r.status !== 404) throw failed(r, "reading the server's sign-in endpoints");
  const meta = r.status === 200 ? json(r) : {};
  if (!("device_authorization_endpoint" in meta)) {
    // It signed this session in, so a previous build answered: ask again later.
    if (signedIn)
      throw new BackendUnavailable(
        `${server} answered from a build without \`loom login\`; retry (if it no longer signs in, run \`loom logout\`)`,
      );
    throw new LoomError(`${server} has no sign-in: it runs without auth (no login needed) or predates \`loom login\``);
  }
  const out: Partial<Endpoints> = {};
  for (const name of ["device_authorization_endpoint", "token_endpoint", "revocation_endpoint"] as const) {
    const url = String(meta[name] ?? "");
    const at = origin(url);
    if (at === undefined || at !== origin(server))
      throw new LoomError(`${server} signs in at ${url}; use that as the server`);
    out[name] = url;
  }
  return out as Endpoints;
}

function hex(n: NodeModules, bytes: number): string {
  return n.crypto.randomBytes(bytes).toString("hex");
}

/**
 * Sign in to `server` (default `LOOM_BASE_URL`, else localhost) and store
 * the tokens under the profile. Resolves once the sign-in is approved, and
 * rejects when it is denied or expires (ten minutes). Answers what `whoami()` does.
 */
export async function login(server?: string, opts: LoginOptions = {}): Promise<WhoAmI> {
  const n = await nodeOrThrow();
  const base = (server || envVar("LOOM_BASE_URL") || DEFAULT_SERVER).trim().replace(/\/+$/, "");
  const profile = opts.profile || defaultProfile();
  const echo = opts.echo ?? ((line: string) => console.log(line));
  const meta = await endpoints(opts.fetch, base);
  const r = await exchange(opts.fetch, meta.device_authorization_endpoint, { form: { client_id: CLIENT_ID } });
  if (r.status !== 200) throw failed(r, "starting the sign-in");
  const grant = json(r);
  const missing = ["device_code", "user_code", "verification_uri", "verification_uri_complete"].filter(
    (k) => !(k in grant),
  );
  if (missing.length) throw new LoomError(`${base} answered a sign-in without ${missing.join(", ")}`);
  echo(
    `To sign in, open ${grant.verification_uri} and enter the code ${grant.user_code}\n` +
      `(or open ${grant.verification_uri_complete})`,
  );
  if (opts.openBrowser !== false && proc()?.stdout?.isTTY) openBrowser(n, String(grant.verification_uri_complete));
  const answer = await poll(opts, meta.token_endpoint, grant, r.headers.has(SESSIONS_HEADER));
  // Another process may be rotating a profile in the same file.
  const replaced = await locked((m) => {
    const before = read(m).profiles[profile];
    save(m, profile, base, json(answer), hex(m, 8), answer.headers.has(SESSIONS_HEADER));
    return before;
  });
  if (replaced) await endSession(opts.fetch, replaced); // the session this sign-in replaces ends too
  const info = await whoami(profile, opts);
  // Said every time: a code someone else approved signs you in as them.
  echo(`Signed in to ${base} as ${info.subject} (${info.org}).`);
  return info;
}

/** The platform's opener, detached; a machine with none just shows the URL. */
function openBrowser(n: NodeModules, url: string): void {
  if (!/^https?:\/\//i.test(url)) return;
  const platform = proc()?.platform;
  // rundll32 takes the URL as one argument, where `start` would hand it to cmd's parser.
  const [cmd, args] =
    platform === "darwin"
      ? ["open", [url]]
      : platform === "win32"
        ? ["rundll32", ["url.dll,FileProtocolHandler", url]]
        : ["xdg-open", [url]];
  try {
    const child = n.childProcess.spawn(cmd, args, { detached: true, stdio: "ignore" });
    child.on("error", () => undefined);
    child.unref();
  } catch {
    /* no opener */
  }
}

async function poll(
  opts: LoginOptions,
  tokenEndpoint: string,
  grant: Record<string, unknown>,
  marked: boolean,
): Promise<Answer> {
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let interval = Number(grant.interval ?? 5);
  const deadline = performance.now() + Number(grant.expires_in ?? 600) * 1000;
  while (performance.now() < deadline) {
    await sleep(interval * 1000);
    let r: Answer;
    try {
      r = await exchange(opts.fetch, tokenEndpoint, {
        form: { grant_type: DEVICE_GRANT, device_code: String(grant.device_code), client_id: CLIENT_ID },
      });
    } catch (e) {
      // Not approved yet, keep asking. (Had the approved answer been the
      // one lost, the next poll is refused and login says to run again.)
      if (e instanceof BackendUnavailable) continue;
      throw e;
    }
    if (r.status >= 500) continue;
    if (r.status === 200) return r;
    if (marked && !r.headers.has(SESSIONS_HEADER)) continue; // a previous build's: the grant lives on the one that started it
    const error = oauthField(r, "error");
    if (error === "authorization_pending") continue;
    if (error === "slow_down") {
      interval += 5; // RFC 8628 §3.5
      continue;
    }
    if (error === "access_denied") throw new AuthError("the sign-in was denied", { status: r.status, code: error });
    if (error === "expired_token") break;
    throw failed(r, "sign-in failed");
  }
  throw new AuthError("the sign-in code expired; run `loom login` again");
}

function usable(entry: Entry, refused: string): boolean {
  return Number(entry.expires_at) - REFRESH_MARGIN_S > Date.now() / 1000 && entry.access_token !== refused;
}

/**
 * The profile's tokens, which must still be the ones for `server` (when
 * given): a profile signed in again to another server never lends its
 * token to a client made for the first.
 */
function entryFor(n: NodeModules, profile: string, server?: string): Entry {
  const entry = read(n).profiles[profile];
  if (!entry) throw new AuthError(`not signed in (profile '${profile}'); run \`loom login\``);
  if (server !== undefined && entry.server !== server)
    throw new AuthError(`profile '${profile}' is now signed in to ${entry.server}, not ${server}`);
  return { ...entry };
}

/** Rotate the profile's tokens. Called with the lock held. */
async function refresh(
  n: NodeModules,
  f: FetchFn | undefined,
  profile: string,
  refused: string,
  server?: string,
): Promise<Entry> {
  const entry = entryFor(n, profile, server); // read again: another process may have changed it
  if (usable(entry, refused)) return entry; // another process refreshed it while we waited
  if (!entry.refresh_token) throw new AuthError(`the session for profile '${profile}' ended; run \`loom login\``);
  const form = { grant_type: "refresh_token", refresh_token: entry.refresh_token, client_id: CLIENT_ID };
  const endpoint = (await endpoints(f, entry.server, true)).token_endpoint;
  let r: Answer;
  try {
    r = await exchange(f, endpoint, { form }, REFRESH_TIMEOUT_MS);
  } catch (e) {
    // Nothing reached the server, so nothing was spent: try once more. (A
    // lost answer cannot be asked again: the server spent the token, and
    // presenting it twice ends the session.)
    if (!neverConnected(e)) throw e;
    r = await exchange(f, endpoint, { form }, REFRESH_TIMEOUT_MS);
  }
  const marked = Boolean(entry.marked);
  if (marked && r.status >= 400 && r.status < 500 && !r.headers.has(SESSIONS_HEADER))
    throw new BackendUnavailable(
      `${entry.server} answered from a build that predates this session; retry, or run \`loom login\` if it persists`,
      { status: r.status },
    );
  if (r.status === 400 && oauthField(r, "error") === "invalid_grant") {
    drop(n, profile);
    let why = oauthField(r, "error_description");
    // Maybe a stolen copy: what it could have minted is the owner's to check.
    if (why.includes("presented again"))
      why += "; if you did not just lose a refresh, check `loom call /v1/me/service_accounts`";
    throw new AuthError(`the session for profile '${profile}' ended (${why || "expired"}); run \`loom login\``, {
      status: 400,
      code: "invalid_grant",
    });
  }
  // A 503 spent nothing (the identity provider could not be asked): retry later.
  if (r.status !== 200) throw failed(r, `refreshing the session at ${entry.server}`);
  // The marker, seen once, is kept: a previous build's refusal must not end it later.
  save(n, profile, entry.server, json(r), entry.session ?? "", marked || r.headers.has(SESSIONS_HEADER));
  return { ...read(n).profiles[profile] };
}

/**
 * A live access token for the profile, refreshed first when it is about to
 * expire or is the one the server just `refused`. With `server`, only while
 * the profile still signs in there.
 */
export async function accessToken(
  profile?: string,
  opts: CredentialOptions & { refused?: string; server?: string } = {},
): Promise<string> {
  const name = profile || defaultProfile();
  const refused = opts.refused ?? "";
  let entry = entryFor(await nodeOrThrow(), name, opts.server);
  if (!usable(entry, refused)) {
    try {
      entry = await locked((n) => refresh(n, opts.fetch, name, refused, opts.server));
    } catch (e) {
      // Early, not yet expired, and not the one refused: it still works.
      if (!(e instanceof BackendUnavailable) || entry.access_token === refused || entry.expires_at <= Date.now() / 1000)
        throw e;
    }
  }
  return entry.access_token;
}

/** Drop the profile's tokens; with `session`, only while the profile still holds that sign-in. */
async function forget(profile: string, session?: string): Promise<void> {
  await locked((n) => {
    const entry = read(n).profiles[profile];
    if (entry && (session === undefined || (entry.session ?? "") === session)) drop(n, profile);
  });
}

/**
 * End the profile's session on the server and forget it here. Answers
 * whether the server confirmed; the local tokens are gone either way.
 */
export async function logout(profile?: string, opts: CredentialOptions = {}): Promise<boolean> {
  const name = profile || defaultProfile();
  const entry = read(await nodeOrThrow()).profiles[name];
  if (!entry) return false;
  try {
    return await endSession(opts.fetch, entry);
  } finally {
    // A login made meanwhile is a new session: it stays.
    await forget(name, entry.session ?? "");
  }
}

/** End a stored session at the server it signed in to; whether it confirmed. */
async function endSession(f: FetchFn | undefined, entry: Entry): Promise<boolean> {
  try {
    const meta = await endpoints(f, entry.server);
    const r = await exchange(f, meta.revocation_endpoint, {
      form: { token: entry.refresh_token || entry.access_token, client_id: CLIENT_ID },
    });
    return r.status === 200;
  } catch {
    return false;
  }
}

/** Who the profile's token says you are, as the server verifies it. */
export async function whoami(profile?: string, opts: CredentialOptions = {}): Promise<WhoAmI> {
  const name = profile || defaultProfile();
  const server = entryFor(await nodeOrThrow(), name).server;
  const token = await accessToken(name, { ...opts, server }); // this server's token, or none
  const r = await exchange(opts.fetch, `${server}/v1/whoami`, { headers: { Authorization: `Bearer ${token}` } });
  if (r.status !== 200) throw failed(r, "asking the server who you are");
  return { profile: name, server, ...json(r) } as WhoAmI;
}

/**
 * The server `profile` signed in to, read synchronously for a client's
 * constructor; undefined when it has not. Throws on a runtime that cannot
 * read files synchronously.
 */
export function signedInServer(profile: string): string | undefined {
  const n = nodeNow();
  if (!n) throw new LoomError("the `profile` option needs Node 20.16 or later (process.getBuiltinModule)");
  return read(n).profiles[profile]?.server;
}

/** The signed-in profile when it signed in to `server`, else undefined: a profile's token is never sent anywhere else. */
export async function storedLogin(
  server: string,
  profile?: string,
  opts: CredentialOptions = {},
): Promise<StoredLogin | undefined> {
  const n = await node();
  const name = profile || defaultProfile();
  const entry = n ? read(n).profiles[name] : undefined;
  if (!entry || !sameServer(entry.server, server)) return undefined;
  return new StoredLogin(name, entry.server, opts);
}

/**
 * Sends the profile's access token, refreshing it before it expires and once
 * more when the server answers 401. Bound to the server it was made for:
 * every read and refresh checks the profile still signs in there, and the
 * transport sends it to that origin only.
 */
export class StoredLogin implements TokenProvider {
  readonly profile: string;
  readonly server: string;
  readonly label: string;
  readonly origin: string;
  readonly #fetch: FetchFn | undefined;
  #token = "";

  constructor(profile: string, server: string, opts: CredentialOptions = {}) {
    this.profile = profile;
    this.server = server.replace(/\/+$/, "");
    this.label = `login:${profile}`;
    this.origin = new URL(this.server).origin;
    this.#fetch = opts.fetch;
  }

  current(): string | undefined {
    return this.#token || undefined;
  }

  async token(refused = ""): Promise<string> {
    this.#token = await accessToken(this.profile, { refused, server: this.server, fetch: this.#fetch });
    return this.#token;
  }

  /** What JSON.stringify and inspect show: never the token. */
  toJSON(): Record<string, unknown> {
    return { profile: this.profile, server: this.server };
  }
}

/** The first line of `~/.loom/token` (what `loom mint-token --save` writes), or undefined. */
async function tokenFromFile(): Promise<string | undefined> {
  const n = await node();
  if (!n) return undefined;
  let text: string;
  try {
    text = n.fs.readFileSync(n.path.join(loomDir(n), "token"), "utf8");
  } catch {
    return undefined;
  }
  return text.trim().split(/\r?\n/)[0]?.trim() || undefined;
}

/**
 * The last two credentials a client looks for, decided on its first request
 * (the constructor cannot read files): the stored login of `LOOM_PROFILE`
 * (else `default`) when it signed in to `baseUrl`, else `~/.loom/token`, else
 * none, when the request goes out with no Authorization header.
 */
export class LocalCredential implements TokenProvider {
  readonly origin: string;
  readonly #baseUrl: string;
  readonly #fetch: FetchFn | undefined;
  #found: Promise<StoredLogin | string> | undefined;
  #chosen: StoredLogin | string | undefined;

  constructor(baseUrl: string, opts: CredentialOptions = {}) {
    this.#baseUrl = baseUrl;
    this.origin = new URL(baseUrl).origin;
    this.#fetch = opts.fetch;
  }

  get label(): string {
    const c = this.#chosen;
    if (c === undefined) return "stored login or ~/.loom/token";
    if (typeof c === "string") return c ? "~/.loom/token" : "none";
    return c.label;
  }

  current(): string | undefined {
    const c = this.#chosen;
    return typeof c === "string" ? c || undefined : c?.current();
  }

  async token(refused = ""): Promise<string> {
    this.#found ??= (async () => {
      const found =
        (await storedLogin(this.#baseUrl, undefined, { fetch: this.#fetch })) ?? (await tokenFromFile()) ?? "";
      this.#chosen = found;
      return found;
    })();
    const c = await this.#found;
    return typeof c === "string" ? c : c.token(refused);
  }

  toJSON(): Record<string, unknown> {
    return { credential: this.label };
  }
}
