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

import {
  AuthError,
  BackendUnavailable,
  busy,
  classed,
  connectFailed,
  LoomError,
  NETWORK_ERROR_CODE,
} from "./errors.js";
import type { TokenProvider } from "./machine.js";
import { envVar, proc } from "./runtime.js";

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
/**
 * Discovery, the exchange and its one retry together, so a refresh holds the
 * lock for a bounded time: each request's timeout is what the deadline leaves,
 * and covers the whole request, its answer's body included.
 */
export const REFRESH_DEADLINE_MS = 90_000;
// The lock both SDKs take. Its holder touches the directory every heartbeat;
// one silent for the stale age is broken, and so is one silent for two
// heartbeats whose holder on this machine is no longer running.
const LOCK_DIR = ".credentials.lock.d";
export const LOCK_HEARTBEAT_MS = 10_000;
export const LOCK_SILENT_MS = 2 * LOCK_HEARTBEAT_MS;
export const LOCK_STALE_MS = 60_000;
// How long a release waits for a breaker's claim to go.
export const RELEASE_WAIT_MS = 2_000;
// What Windows refuses a rename with while another process has a file in the
// directory open, as a waiter reading the owner file does for a moment.
const BUSY = new Set(["EACCES", "EPERM", "EBUSY"]);
export const RENAME_TRIES = 5;
export const RENAME_WAIT_MS = 20;
/** How often a release asks again for the claim a breaker holds. */
export const CLAIM_POLL_MS = 20;

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
  if (!isObject(data) || !isObject(profiles)) return { profiles: {} };
  return data as CredentialsFile;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
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
      await release();
    }
  });
  inProcess = run.catch(() => undefined);
  return run;
}

/**
 * Take `~/.loom/.credentials.lock.d`, a directory whose owner file names the
 * holder, and touch it every heartbeat while held. Waits while another
 * process holds it, and breaks it once stale. Returns the release, which
 * also runs, blocking, if the process exits holding it.
 */
async function takeLockDir(n: NodeModules): Promise<() => Promise<void>> {
  const dir = loomDir(n);
  n.fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const lock = n.path.join(dir, LOCK_DIR);
  const owner = JSON.stringify({ pid: proc()?.pid, host: n.os.hostname(), time: Date.now() / 1000 });
  sweep(n, lock);
  let wait = 10;
  while (!(await waitAsync(createLock(n, lock, owner)))) {
    const held = ownerOf(n, lock);
    if (stale(n, lock, held)) await waitAsync(breakLock(n, lock, held));
    await new Promise((resolve) => setTimeout(resolve, wait));
    wait = Math.min(wait * 2, 200);
  }
  const beat = setInterval(() => {
    if (ownerOf(n, lock) !== owner) return;
    try {
      touch(n, lock);
    } catch {
      /* released meanwhile */
    }
  }, LOCK_HEARTBEAT_MS);
  beat.unref?.();
  // A release never fails the refresh it ends: a lock it cannot remove is left for the stale age.
  // An exit during the release finishes that one: a second would find its own claim taken.
  let releasing: Waiting<void> | undefined;
  const unhook = releaseOnExit(() => {
    clearInterval(beat);
    unhook();
    try {
      waitSync(releasing ?? releaseLock(n, lock, owner));
    } catch {
      /* left for the stale age */
    }
  });
  return async () => {
    clearInterval(beat);
    releasing = releaseLock(n, lock, owner);
    try {
      await waitAsync(releasing);
    } catch {
      /* left for the stale age */
    } finally {
      unhook(); // after: an exit while this waits still releases
    }
  };
}

/** A step that may wait: it yields each wait, in milliseconds, before it goes on. */
type Waiting<T> = Generator<number, T, void>;

/** Run `steps` waiting on a timer, so the event loop goes on meanwhile. */
async function waitAsync<T>(steps: Waiting<T>): Promise<T> {
  for (;;) {
    const step = steps.next();
    if (step.done) return step.value;
    await new Promise((resolve) => setTimeout(resolve, step.value));
  }
}

/** Run `steps` blocking for each wait: at exit nothing waits on a timer. */
function waitSync<T>(steps: Waiting<T>): T {
  for (;;) {
    const step = steps.next();
    if (step.done) return step.value;
    sleepSync(step.value);
  }
}

/**
 * Take `lock` by renaming a private directory that holds the owner file
 * onto it. The rename fails while the lock exists, and a lock never exists
 * without its owner. Answers whether it was taken.
 */
function* createLock(n: NodeModules, lock: string, owner: string): Waiting<boolean> {
  const tmp = `${lock}.${hex(n, 6)}.tmp`;
  n.fs.mkdirSync(tmp, { mode: 0o700 });
  try {
    n.fs.writeFileSync(n.path.join(tmp, "owner"), owner, { mode: 0o600 });
    touch(n, tmp);
    for (let tries = 1; ; tries++) {
      try {
        n.fs.renameSync(tmp, lock);
        return true;
      } catch (e) {
        // POSIX allows no rename onto a non-empty directory, and Windows
        // none onto any directory, refused as access denied: held while the
        // lock is there. With no lock, the refusal is a scanner holding a
        // file in tmp open, or a holder renaming the lock away meanwhile:
        // tried again, then raised.
        const code = errorCode(e);
        if (code === "EEXIST" || code === "ENOTEMPTY") return false;
        if (!BUSY.has(code ?? "")) throw e;
        if (n.fs.existsSync(lock)) return false;
        if (tries === RENAME_TRIES) {
          throw new LoomError(`cannot take the credentials lock at ${lock} (${code})`, { cause: e });
        }
        yield RENAME_WAIT_MS;
      }
    }
  } finally {
    removeQuietly(n, tmp); // gone already once renamed
  }
}

function errorCode(e: unknown): string | undefined {
  return (e as { code?: string } | null)?.code;
}

/** Delete `path` and all in it, ignoring a failure, as Python's ignore_errors does. */
function removeQuietly(n: NodeModules, path: string): void {
  try {
    n.fs.rmSync(path, { recursive: true, force: true, maxRetries: 5 });
  } catch {
    /* left for the sweep */
  }
}

/** This machine's clock: on a network home a file's mtime is otherwise the file server's. */
function touch(n: NodeModules, path: string): void {
  const now = new Date();
  n.fs.utimesSync(path, now, now);
}

/**
 * Run `release` if the process ends while holding the lock: at exit, and on
 * SIGINT or SIGTERM when nothing else listens for it. The signal is then
 * raised again, so the process ends as it would have; with a listener of
 * its own, that listener decides, and an exit it makes releases. Returns
 * the unhook.
 */
function releaseOnExit(release: () => void): () => void {
  const p = proc();
  const { on, off } = p ?? {};
  if (!p || typeof on !== "function" || typeof off !== "function") return () => undefined;
  const signals = ["SIGINT", "SIGTERM"];
  const onSignal = (signal: string) => {
    if (p.listenerCount?.(signal) !== 1) return;
    release(); // which unhooks this listener, restoring the signal's default
    if (p.pid !== undefined) p.kill?.(p.pid, signal);
  };
  on.call(p, "exit", release);
  for (const s of signals) on.call(p, s, onSignal);
  return () => {
    off.call(p, "exit", release);
    for (const s of signals) off.call(p, s, onSignal);
  };
}

/** The owner file's text; empty when the lock is gone. */
function ownerOf(n: NodeModules, lock: string): string {
  try {
    return n.fs.readFileSync(n.path.join(lock, "owner"), "utf8");
  } catch {
    return "";
  }
}

/**
 * Whether the holder of `lock` is gone: silent for the stale age (a holder
 * on another machine, a reused pid, a hung process), or silent for two
 * heartbeats and no longer running on this machine. The pid is asked only
 * after the heartbeat stopped: a holder in another pid namespace on this
 * host (a container sharing the home directory) looks dead to it.
 */
function stale(n: NodeModules, lock: string, held: string): boolean {
  const age = ageMs(n, lock);
  if (age > LOCK_STALE_MS) return true;
  if (age <= LOCK_SILENT_MS) return false;
  let owner: unknown;
  try {
    owner = JSON.parse(held);
  } catch {
    return false;
  }
  if (!isObject(owner) || owner.host !== n.os.hostname()) return false; // another machine's pid means nothing here
  const pid = owner.pid;
  return Number.isInteger(pid) && (pid as number) > 0 && !running(pid as number);
}

function running(pid: number): boolean {
  try {
    proc()?.kill?.(pid, 0);
    return true;
  } catch (e) {
    return errorCode(e) !== "ESRCH"; // EPERM: running, as another user
  }
}

/**
 * Remove the stale lock whose owner file read `held`. A claim directory
 * beside it lets one waiter at a time do it, or the holder release it, and
 * the owner is read again under the claim, so a lock taken over meanwhile
 * is left alone.
 */
function* breakLock(n: NodeModules, lock: string, held: string): Waiting<void> {
  const claim = `${lock}.breaking`;
  try {
    n.fs.mkdirSync(claim, { mode: 0o700 });
  } catch (e) {
    if (errorCode(e) === "EEXIST") {
      // Left by a process that died holding it: once as old as a stale
      // lock, renamed away for the sweep to delete.
      if (ageMs(n, claim) > LOCK_STALE_MS) {
        try {
          n.fs.renameSync(claim, `${claim}.${hex(n, 6)}.stale`);
        } catch {
          /* cleared by another waiter */
        }
      }
      return;
    }
    if (errorCode(e) === "ENOENT") return; // the directory holding it is gone: try again
    throw unbreakable(lock, e);
  }
  try {
    try {
      touch(n, claim);
    } catch {
      /* aged by the file server's clock instead */
    }
    if (ownerOf(n, lock) === held) yield* removeLock(n, lock);
  } catch (e) {
    // Busy on Windows: try again after the wait, until the lock is twice
    // the stale age, so a lock that is never renamable still raises.
    if (BUSY.has(errorCode(e) ?? "") && ageMs(n, lock) <= 2 * LOCK_STALE_MS) return;
    throw unbreakable(lock, e);
  } finally {
    rmdirQuietly(n, claim);
  }
}

/**
 * Remove our own lock, holding the claim a breaker takes: a breaker that
 * judged the lock stale renames it under the claim, and a release between
 * its check and its rename would let a third process take the path that
 * breaker then renames away. Without the claim in time the lock is left,
 * never renamed, and broken once its heartbeat has stopped.
 */
function* releaseLock(n: NodeModules, lock: string, owner: string): Waiting<void> {
  if (ownerOf(n, lock) !== owner) return; // broken as stale: another process's by now
  const claim = `${lock}.breaking`;
  const deadline = performance.now() + RELEASE_WAIT_MS;
  for (;;) {
    try {
      n.fs.mkdirSync(claim, { mode: 0o700 });
      break;
    } catch (e) {
      if (errorCode(e) !== "EEXIST") throw e;
      if (performance.now() > deadline) return;
      yield CLAIM_POLL_MS;
    }
  }
  try {
    try {
      touch(n, claim); // as a breaker's is, so it never looks old to the next one
    } catch {
      /* aged by the file server's clock instead */
    }
    if (ownerOf(n, lock) === owner) yield* removeLock(n, lock);
  } finally {
    rmdirQuietly(n, claim);
  }
}

/** Block for `ms`: for a release at exit or on a signal, where no timer fires. */
function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function unbreakable(lock: string, e: unknown): LoomError {
  const why = errorCode(e) ?? String(e);
  return new LoomError(`the credentials lock at ${lock} is stale but cannot be broken (${why}); remove it by hand`, {
    cause: e,
  });
}

function rmdirQuietly(n: NodeModules, path: string): void {
  try {
    n.fs.rmdirSync(path);
  } catch {
    /* gone, or not ours to remove */
  }
}

/**
 * Rename `lock` away, then delete it: the lock path is never a half-removed
 * directory, empty, which a rename could replace.
 */
function* removeLock(n: NodeModules, lock: string): Waiting<void> {
  const tomb = `${lock}.${hex(n, 6)}.stale`;
  for (let attempt = 0; ; attempt++) {
    try {
      n.fs.renameSync(lock, tomb);
      break;
    } catch (e) {
      if (errorCode(e) === "ENOENT") return; // released meanwhile
      if (!BUSY.has(errorCode(e) ?? "") || attempt === RENAME_TRIES - 1) throw e;
      yield RENAME_WAIT_MS; // a waiter reading the owner file closes it in a moment
    }
  }
  removeQuietly(n, tomb);
}

/**
 * Delete what a process that died taking or breaking the lock left beside
 * it: its private directory, or the lock or claim it renamed away.
 */
function sweep(n: NodeModules, lock: string): void {
  const prefix = `${n.path.basename(lock)}.`;
  for (const name of n.fs.readdirSync(n.path.dirname(lock))) {
    const left = n.path.join(n.path.dirname(lock), name);
    if (!name.startsWith(prefix) || !/\.(tmp|stale)$/.test(name)) continue;
    if (ageMs(n, left) > LOCK_STALE_MS) removeQuietly(n, left);
  }
}

function ageMs(n: NodeModules, path: string): number {
  try {
    return Date.now() - n.fs.statSync(path).mtimeMs;
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
    // The sign-in request was not answered: asked again, as the transport does.
    const err = new BackendUnavailable(`cannot reach ${origin(url) ?? url}: ${why}`, {
      cause: e,
      code: NETWORK_ERROR_CODE,
    });
    throw classed(err, "dependency", true);
  }
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
  if (r.status === 429 || r.status >= 500) return busy(`${doing}: ${error}; retry`, opts);
  if (r.status === 401 || r.status === 403 || error === "invalid_grant")
    return new AuthError(`${doing}: ${error}; run \`loom login\` again`, opts);
  return new LoomError(`${doing}: ${error}`, opts);
}

type Endpoints = Record<"device_authorization_endpoint" | "token_endpoint" | "revocation_endpoint", string>;

/** The server's sign-in endpoints, each checked to be on the server itself. */
async function endpoints(
  f: FetchFn | undefined,
  server: string,
  signedIn = false,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<Endpoints> {
  const r = await exchange(f, `${server}/.well-known/oauth-authorization-server`, {}, timeoutMs);
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
  let href: string;
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return;
    href = u.href;
  } catch {
    return;
  }
  const platform = proc()?.platform;
  // rundll32 takes the URL as one argument, where `start` would hand it to cmd's parser.
  const [cmd, args] =
    platform === "darwin"
      ? ["open", [href]]
      : platform === "win32"
        ? ["rundll32", ["url.dll,FileProtocolHandler", href]]
        : ["xdg-open", [href]];
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
  const deadline = performance.now() + REFRESH_DEADLINE_MS;
  // At most `usual`, and no more than the deadline leaves; nothing left, nothing is sent.
  const left = (usual: number): number => {
    const remaining = deadline - performance.now();
    if (remaining <= 0)
      throw new BackendUnavailable(
        `refreshing the session at ${entry.server} took over ${REFRESH_DEADLINE_MS / 1000} s; retry`,
      );
    return Math.min(usual, remaining);
  };
  const endpoint = (await endpoints(f, entry.server, true, left(REQUEST_TIMEOUT_MS))).token_endpoint;
  let r: Answer;
  try {
    r = await exchange(f, endpoint, { form }, left(REFRESH_TIMEOUT_MS));
  } catch (e) {
    // Nothing reached the server, so nothing was spent: try once more. (A
    // lost answer cannot be asked again: the server spent the token, and
    // presenting it twice ends the session.)
    if (!connectFailed(e)) throw e;
    r = await exchange(f, endpoint, { form }, left(REFRESH_TIMEOUT_MS));
  }
  const marked = Boolean(entry.marked);
  if (marked && r.status >= 400 && r.status < 500 && !r.headers.has(SESSIONS_HEADER))
    throw classed(
      new BackendUnavailable(
        `${entry.server} answered from a build that predates this session; retry, or run \`loom login\` if it persists`,
        { status: r.status },
      ),
      "dependency",
      true,
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
  if (!n)
    throw new LoomError(
      "the `profile` option needs process.getBuiltinModule (Node 20.16 or later, 22.3 or later on Node 22)",
    );
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
