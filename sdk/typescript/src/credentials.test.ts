import childProcess, { spawnSync } from "node:child_process";
import fs, {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { rm } from "node:fs/promises";
import { hostname, tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readEnv } from "./client.js";
import {
  accessToken,
  type CredentialOptions,
  defaultProfile,
  LOCK_HEARTBEAT_MS,
  LOCK_SILENT_MS,
  LOCK_STALE_MS,
  LocalCredential,
  type LoginOptions,
  login,
  logout,
  RELEASE_WAIT_MS,
  RENAME_TRIES,
  RENAME_WAIT_MS,
  StoredLogin,
  sameServer,
  type WhoAmI,
  whoami,
} from "./credentials.js";
import { AuthError, BackendUnavailable, LoomError } from "./errors.js";
import { MachineLogin } from "./machine.js";
import { Transport } from "./transport.js";

const SERVER = "https://loom.example";
const META = {
  device_authorization_endpoint: `${SERVER}/oauth/device_authorization`,
  token_endpoint: `${SERVER}/oauth/token`,
  revocation_endpoint: `${SERVER}/oauth/revoke`,
};

let home = "";

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), "loom-creds-"));
  vi.stubEnv("HOME", home);
  vi.stubEnv("USERPROFILE", home);
  for (const name of ["LOOM_PROFILE", "LOOM_BASE_URL", "LOOM_API_KEY", "LOOM_CLIENT_ID", "LOOM_WORKLOAD_TOKEN"])
    vi.stubEnv(name, "");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  rmSync(home, { recursive: true, force: true });
});

type Scripted = { status: number; body?: unknown; previousBuild?: boolean };
type Sent = { url: string; path: string; form: URLSearchParams; auth: string | null };

/**
 * A fake Loom: discovery, the device grant (pending once, then approved),
 * refresh, revoke, whoami and one op; records every request.
 */
function fakeServer() {
  const s = {
    sent: [] as Sent[],
    queued: new Map<string, Array<Scripted | Error>>(),
    meta: META as Record<string, string>,
    pending: 1,
    marks: true,
    n: 0,
    /** The access token `/v1/memory.get` accepts; undefined accepts any. */
    valid: undefined as string | undefined,
    onRefresh: undefined as (() => Promise<void>) | undefined,
    queue(path: string, ...answers: Array<Scripted | Error>) {
      s.queued.set(path, [...(s.queued.get(path) ?? []), ...answers]);
    },
    paths(): string[] {
      return s.sent.map((c) => c.path);
    },
    fetch: async (url: string, init: RequestInit): Promise<Response> => {
      const u = new URL(url);
      const form = new URLSearchParams(typeof init.body === "string" ? init.body : "");
      const auth = new Headers(init.headers).get("authorization");
      s.sent.push({ url, path: u.pathname, form, auth });
      const a = await answer(u, form, auth);
      const marked =
        s.marks && !a.previousBuild && ["/oauth/token", "/oauth/device_authorization"].includes(u.pathname);
      return new Response(JSON.stringify(a.body ?? {}), {
        status: a.status,
        headers: marked ? { "Loom-Sessions": "1" } : {},
      });
    },
  };
  const tokens = () => {
    s.n++;
    return { status: 200, body: { access_token: `a${s.n}`, refresh_token: `r${s.n}`, expires_in: 3600 } };
  };
  async function answer(u: URL, form: URLSearchParams, auth: string | null): Promise<Scripted> {
    const next = s.queued.get(u.pathname)?.shift();
    if (next instanceof Error) throw next;
    if (next) return next;
    switch (u.pathname) {
      case "/.well-known/oauth-authorization-server":
        return { status: 200, body: s.meta };
      case "/oauth/device_authorization":
        return {
          status: 200,
          body: {
            device_code: "dc",
            user_code: "BCDF-GHJK",
            verification_uri: `${SERVER}/oauth/device`,
            verification_uri_complete: `${SERVER}/oauth/device?user_code=BCDF-GHJK`,
            expires_in: 600,
            interval: 0,
          },
        };
      case "/oauth/token":
        if (form.get("grant_type")?.endsWith("device_code") && s.pending) {
          s.pending--;
          return { status: 400, body: { error: "authorization_pending" } };
        }
        if (form.get("grant_type") === "refresh_token") await s.onRefresh?.();
        return tokens();
      case "/oauth/revoke":
        return { status: 200 };
      case "/v1/whoami":
        return { status: 200, body: { subject: "ada", org: "acme" } };
      case "/v1/memory.get":
        if (s.valid !== undefined && auth !== `Bearer ${s.valid}`) return { status: 401, body: { detail: {} } };
        return { status: 200, body: { found: true } };
    }
    throw new Error(`unexpected ${u}`);
  }
  return s;
}

type Profile = {
  server?: string;
  session?: string;
  marked?: boolean;
  access_token?: string;
  refresh_token?: string;
  expires_at?: number;
};

function credentialsPath(): string {
  return join(home, ".loom", "credentials");
}

function profiles(): Record<string, Required<Profile>> {
  try {
    return JSON.parse(readFileSync(credentialsPath(), "utf8")).profiles;
  } catch {
    return {};
  }
}

/** Store a profile as `loom login` would have. */
function store(name: string, p: Profile = {}): void {
  mkdirSync(join(home, ".loom"), { recursive: true, mode: 0o700 });
  const all = profiles();
  all[name] = {
    server: SERVER,
    session: "s0",
    marked: true,
    access_token: "a0",
    refresh_token: "r0",
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    ...p,
  };
  writeFileSync(credentialsPath(), JSON.stringify({ profiles: all }), { mode: 0o600 });
}

const now = () => Math.floor(Date.now() / 1000);
const noSleep = async () => undefined;

describe("login", () => {
  it("runs the device flow, stores the tokens 0600 under the profile and answers whoami", async () => {
    const srv = fakeServer();
    const lines: string[] = [];
    const opts: LoginOptions = { fetch: srv.fetch, echo: (l) => lines.push(l), sleep: noSleep };
    const info: WhoAmI = await login(SERVER, opts);
    expect(info).toMatchObject({ profile: "default", server: SERVER, subject: "ada", org: "acme" });
    expect(lines[0]).toContain("BCDF-GHJK");
    expect(lines[1]).toBe(`Signed in to ${SERVER} as ada (acme).`);
    const polls = srv.sent.filter((c) => c.path === "/oauth/token");
    expect(polls).toHaveLength(2);
    expect(Object.fromEntries(polls[0].form)).toEqual({
      grant_type: "urn:ietf:params:oauth:grant-type:device_code",
      device_code: "dc",
      client_id: "loom-cli",
    });
    const p = profiles().default;
    expect(p).toMatchObject({ server: SERVER, marked: true, access_token: "a1", refresh_token: "r1" });
    expect(p.session).toMatch(/^[0-9a-f]{16}$/);
    expect(p.expires_at).toBeGreaterThan(now() + 3500);
    expect(statSync(credentialsPath()).mode & 0o777).toBe(0o600);
    expect(statSync(join(home, ".loom")).mode & 0o777).toBe(0o700);
    expect(srv.sent.at(-1)).toMatchObject({ path: "/v1/whoami", auth: "Bearer a1" });
  });

  it("stores under the named profile and LOOM_PROFILE, and defaults the server to LOOM_BASE_URL", async () => {
    const srv = fakeServer();
    vi.stubEnv("LOOM_BASE_URL", `${SERVER}/`);
    vi.stubEnv("LOOM_PROFILE", "work");
    await login(undefined, { fetch: srv.fetch, echo: () => undefined, sleep: noSleep });
    await login(undefined, { profile: "other", fetch: srv.fetch, echo: () => undefined, sleep: noSleep });
    expect(Object.keys(profiles()).sort()).toEqual(["other", "work"]);
    expect(profiles().work.server).toBe(SERVER);
  });

  it("slows down by five seconds on slow_down and keeps asking through 5xx and lost connections", async () => {
    const srv = fakeServer();
    srv.pending = 0;
    srv.queue(
      "/oauth/token",
      { status: 400, body: { error: "slow_down" } },
      { status: 503 },
      new TypeError("fetch failed"),
      { status: 400, body: { detail: { error: "authorization_pending" } } },
    );
    const slept: number[] = [];
    const sleep = async (ms: number) => void slept.push(ms);
    await login(SERVER, { fetch: srv.fetch, echo: () => undefined, sleep });
    expect(slept).toEqual([0, 5000, 5000, 5000, 5000]);
    expect(profiles().default.access_token).toBe("a1");
  });

  it.each([
    ["access_denied", /denied/],
    ["expired_token", /expired; run `loom login` again/],
  ])("%s ends the sign-in with an AuthError and stores nothing", async (error, message) => {
    const srv = fakeServer();
    srv.queue("/oauth/token", { status: 400, body: { error } });
    const done = login(SERVER, { fetch: srv.fetch, echo: () => undefined, sleep: noSleep });
    await expect(done).rejects.toThrow(AuthError);
    await expect(done).rejects.toThrow(message);
    expect(profiles()).toEqual({});
  });

  it("ignores a previous build's refusal once the grant came from a build that marks its answers", async () => {
    const srv = fakeServer();
    srv.pending = 0;
    srv.queue("/oauth/token", { status: 400, body: { error: "invalid_grant" }, previousBuild: true });
    await login(SERVER, { fetch: srv.fetch, echo: () => undefined, sleep: noSleep });
    expect(profiles().default.access_token).toBe("a1");
  });

  it("refuses endpoints on another origin and a server with no sign-in", async () => {
    const srv = fakeServer();
    srv.meta = { ...META, token_endpoint: "https://evil.example/oauth/token" };
    await expect(login(SERVER, { fetch: srv.fetch, echo: () => undefined })).rejects.toThrow(
      /signs in at https:\/\/evil\.example\/oauth\/token; use that as the server/,
    );
    expect(srv.paths()).toEqual(["/.well-known/oauth-authorization-server"]);
    srv.queue("/.well-known/oauth-authorization-server", { status: 404 });
    const done = login(SERVER, { fetch: srv.fetch, echo: () => undefined });
    await expect(done).rejects.toThrow(LoomError);
    await expect(done).rejects.toThrow(/has no sign-in/);
  });

  it("takes a credentials file whose profiles are not an object as empty", async () => {
    mkdirSync(join(home, ".loom"), { recursive: true });
    writeFileSync(credentialsPath(), JSON.stringify({ profiles: [] }));
    await login(SERVER, { fetch: fakeServer().fetch, echo: () => undefined, sleep: noSleep });
    expect(profiles().default).toMatchObject({ access_token: "a1" });
  });

  it("ends the session of the profile it replaces", async () => {
    const srv = fakeServer();
    store("default", { refresh_token: "old-refresh" });
    await login(SERVER, { fetch: srv.fetch, echo: () => undefined, sleep: noSleep });
    const revoke = srv.sent.find((c) => c.path === "/oauth/revoke");
    expect(revoke?.form.get("token")).toBe("old-refresh");
    expect(profiles().default.session).not.toBe("s0");
  });
});

describe("the browser opener", () => {
  let spawned: Array<[string, string[]]> = [];
  const ttyBefore = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
  const platformBefore = Object.getOwnPropertyDescriptor(process, "platform") as PropertyDescriptor;

  function tty(on: boolean): void {
    Object.defineProperty(process.stdout, "isTTY", { value: on, configurable: true });
  }

  function platform(name: string): void {
    Object.defineProperty(process, "platform", { value: name, configurable: true });
  }

  beforeEach(() => {
    spawned = [];
    vi.spyOn(childProcess, "spawn").mockImplementation(((cmd: string, args: string[]) => {
      spawned.push([cmd, args]);
      return { on: () => undefined, unref: () => undefined };
    }) as unknown as typeof childProcess.spawn);
    tty(true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    if (ttyBefore) Object.defineProperty(process.stdout, "isTTY", ttyBefore);
    else delete (process.stdout as { isTTY?: boolean }).isTTY;
    Object.defineProperty(process, "platform", platformBefore);
  });

  /** Sign in with the approval page at `page`. */
  async function signIn(page: string, opts: Partial<LoginOptions> = {}): Promise<void> {
    const srv = fakeServer();
    srv.queue("/oauth/device_authorization", {
      status: 200,
      body: {
        device_code: "dc",
        user_code: "BCDF-GHJK",
        verification_uri: `${SERVER}/oauth/device`,
        verification_uri_complete: page,
        expires_in: 600,
        interval: 0,
      },
    });
    await login(SERVER, { fetch: srv.fetch, echo: () => undefined, sleep: noSleep, ...opts });
  }

  it.each([
    ["darwin", "open", []],
    ["win32", "rundll32", ["url.dll,FileProtocolHandler"]],
    ["linux", "xdg-open", []],
  ])("opens the page on %s with the platform's opener, the URL normalised", async (os, cmd, lead) => {
    platform(os);
    await signIn("HTTPS://loom.example/oauth/device?user_code=BCDF GHJK");
    expect(spawned).toEqual([[cmd, [...lead, "https://loom.example/oauth/device?user_code=BCDF%20GHJK"]]]);
  });

  it("opens nothing but an http(s) URL", async () => {
    for (const page of ["file:///etc/passwd", "javascript:alert(1)", "not a url"]) await signIn(page);
    expect(spawned).toEqual([]);
  });

  it("opens nothing without a terminal or with openBrowser: false", async () => {
    tty(false);
    await signIn(`${SERVER}/oauth/device`);
    tty(true);
    await signIn(`${SERVER}/oauth/device`, { openBrowser: false });
    expect(spawned).toEqual([]);
  });
});

describe("accessToken", () => {
  it("hands over a fresh token without asking the server", async () => {
    const srv = fakeServer();
    store("default");
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a0");
    expect(srv.sent).toHaveLength(0);
  });

  it.each([
    [503, "dependency"],
    [429, "capacity"],
  ] as const)("calls a sign-in answered %s retryable when called directly", async (status, category) => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 10 });
    srv.queue("/oauth/token", { status, body: { error: "temporarily_unavailable" } });
    const err = (await accessToken(undefined, { fetch: srv.fetch }).catch((e: unknown) => e)) as LoomError;
    expect([err.category, err.retryable]).toEqual([category, true]);
  });

  it("refreshes a minute before expiry and keeps the session and the marker", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() + 30, session: "keep" });
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    const refresh = srv.sent.find((c) => c.path === "/oauth/token");
    expect(Object.fromEntries(refresh?.form ?? [])).toEqual({
      grant_type: "refresh_token",
      refresh_token: "r0",
      client_id: "loom-cli",
    });
    expect(profiles().default).toMatchObject({
      session: "keep",
      marked: true,
      access_token: "a1",
      refresh_token: "r1",
    });
  });

  it("refreshes the token the server refused even when it is not about to expire", async () => {
    const srv = fakeServer();
    store("default");
    expect(await accessToken(undefined, { fetch: srv.fetch, refused: "a0" })).toBe("a1");
  });

  it("drops the profile on invalid_grant and says a replayed token may be a stolen copy", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    srv.queue("/oauth/token", {
      status: 400,
      body: { error: "invalid_grant", error_description: "refresh token presented again" },
    });
    const done = accessToken(undefined, { fetch: srv.fetch });
    await expect(done).rejects.toThrow(AuthError);
    await expect(done).rejects.toThrow(/presented again; if you did not just lose a refresh, check/);
    expect(profiles()).toEqual({});
  });

  it("takes a marked session's 4xx without the marker as a previous build's, and keeps the profile", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    srv.queue("/oauth/token", { status: 400, body: { error: "invalid_grant" }, previousBuild: true });
    await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(BackendUnavailable);
    expect(profiles().default.access_token).toBe("a0");
    // Unmarked (a proxy strips the header): the refusal is taken as it is.
    store("default", { expires_at: now() - 1, marked: false });
    srv.marks = false;
    srv.queue("/oauth/token", { status: 400, body: { error: "invalid_grant" } });
    await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(AuthError);
    expect(profiles()).toEqual({});
  });

  it("keeps a still-valid token when the refresh is unavailable, but not an expired or refused one", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() + 30 });
    srv.queue("/oauth/token", { status: 503 }, { status: 503 }, { status: 503 });
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a0");
    await expect(accessToken(undefined, { fetch: srv.fetch, refused: "a0" })).rejects.toThrow(BackendUnavailable);
    store("default", { expires_at: now() - 1 });
    await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(BackendUnavailable);
  });

  it("retries a refresh once when the connection never reached the server, never after a lost answer", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const refused = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    srv.queue("/oauth/token", refused);
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    store("default", { expires_at: now() - 1 });
    const reset = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNRESET" } });
    srv.queue("/oauth/token", reset);
    await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(BackendUnavailable);
    expect(srv.sent.filter((c) => c.path === "/oauth/token")).toHaveLength(3);
  });

  it("refuses a profile signed in again to another server", async () => {
    store("default", { server: "https://other.example" });
    await expect(accessToken(undefined, { server: SERVER })).rejects.toThrow(
      /now signed in to https:\/\/other\.example, not https:\/\/loom\.example/,
    );
    await expect(accessToken("nobody")).rejects.toThrow(/not signed in \(profile 'nobody'\)/);
  });
});

describe("the credentials lock", () => {
  const lockDir = () => join(home, ".loom", ".credentials.lock.d");

  it("lets two concurrent refreshes of one profile make one exchange", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const tokens = await Promise.all([
      accessToken(undefined, { fetch: srv.fetch }),
      new StoredLogin("default", SERVER, { fetch: srv.fetch }).token(),
    ]);
    expect(tokens).toEqual(["a1", "a1"]);
    expect(srv.sent.filter((c) => c.path === "/oauth/token")).toHaveLength(1);
    expect(() => statSync(lockDir())).toThrow(); // released
  });

  it("waits while another process holds the lock directory, then reads the token it stored", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(process.ppid, hostname());
    const done = accessToken(undefined, { fetch: srv.fetch });
    await new Promise((r) => setTimeout(r, 150));
    expect(srv.sent).toHaveLength(0);
    store("default", { access_token: "from-the-other-process" }); // what the holder refreshed to
    await rm(lockDir(), { recursive: true });
    expect(await done).toBe("from-the-other-process");
    expect(srv.sent).toHaveLength(0);
  });

  /** A lock directory another process holds, as its owner file names it. */
  function heldBy(pid: number, host: string, ageMs = 0): void {
    mkdirSync(lockDir(), { recursive: true });
    writeFileSync(join(lockDir(), "owner"), JSON.stringify({ pid, host, time: Date.now() / 1000 }));
    age(lockDir(), ageMs);
  }

  function age(path: string, ms: number): void {
    const at = new Date(Date.now() - ms);
    utimesSync(path, at, at);
  }

  const deadPid = () => spawnSync(process.execPath, ["-e", ""]).pid;
  const leftovers = () => readdirSync(join(home, ".loom")).filter((f) => f.startsWith(".credentials.lock.d."));

  it("breaks a lock whose holder on another machine went silent for the stale age", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    expect(() => statSync(lockDir())).toThrow();
    expect(leftovers()).toEqual([]);
  });

  it("breaks before the stale age a lock that missed two heartbeats and whose holder here stopped", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(deadPid(), hostname(), LOCK_SILENT_MS + 1000);
    const t0 = performance.now();
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    expect(performance.now() - t0).toBeLessThan(2000);
  });

  it.each([
    ["on this machine, running", () => process.ppid, hostname()],
    ["on another machine, heard from recently", deadPid, "another-machine"],
    // A process in another pid namespace (a container sharing the home) looks dead here.
    ["whose pid is not running here, heard from recently", deadPid, hostname()],
  ])("waits for a holder %s", async (_, pid, host) => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(pid(), host);
    const done = accessToken(undefined, { fetch: srv.fetch });
    await new Promise((r) => setTimeout(r, 150));
    expect(srv.sent).toHaveLength(0);
    await rm(lockDir(), { recursive: true });
    expect(await done).toBe("a1");
  });

  it("leaves a stale lock another waiter is breaking", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    mkdirSync(`${lockDir()}.breaking`);
    const done = accessToken(undefined, { fetch: srv.fetch });
    await new Promise((r) => setTimeout(r, 150));
    expect(srv.sent).toHaveLength(0);
    await rm(`${lockDir()}.breaking`, { recursive: true }); // that waiter gave up
    expect(await done).toBe("a1");
    expect(leftovers()).toEqual([]);
  });

  it("clears a claim a breaker left when it died, once as old as a stale lock", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    mkdirSync(`${lockDir()}.breaking`);
    age(`${lockDir()}.breaking`, LOCK_STALE_MS + 1000);
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    // Renamed away, never removed by its path, and deleted by the next sweep.
    expect(leftovers()).toEqual([expect.stringMatching(/^\.credentials\.lock\.d\.breaking\.[0-9a-f]{12}\.stale$/)]);
    store("default", { expires_at: now() - 1 });
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a2");
    expect(leftovers()).toEqual([]);
  });

  it("leaves a fresh claim made right after it cleared an old one", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    const claim = `${lockDir()}.breaking`;
    mkdirSync(claim);
    age(claim, LOCK_STALE_MS + 1000);
    const real = fs.renameSync;
    vi.spyOn(fs, "renameSync").mockImplementation(((from: string, to: string) => {
      real(from, to);
      if (from === claim) mkdirSync(claim); // another waiter's, made right after
    }) as never);
    try {
      const done = accessToken(undefined, { fetch: srv.fetch });
      await new Promise((r) => setTimeout(r, 150));
      expect(existsSync(claim)).toBe(true);
      expect(srv.sent).toHaveLength(0);
      await rm(claim, { recursive: true }); // that waiter gave up
      expect(await done).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
  });

  /** Refuse `path`'s next `times` calls to `fs[call]` with `code`; answers the refusals made. */
  function refuse(call: "renameSync" | "mkdirSync", path: () => string, times: number, code: string): string[] {
    const refused: string[] = [];
    const real = fs[call] as (...a: unknown[]) => unknown;
    vi.spyOn(fs, call).mockImplementation(((p: string, ...rest: unknown[]) => {
      if (p === path() && refused.length < times) {
        refused.push(code);
        throw Object.assign(new Error(code), { code });
      }
      return real(p, ...rest);
    }) as never);
    return refused;
  }

  it.each(["EACCES", "EPERM", "EBUSY"])(
    "retries a release's rename refused with %s while a waiter reads the owner file",
    async (code) => {
      const srv = fakeServer();
      store("default", { expires_at: now() - 1 });
      let refused: string[] = [];
      srv.onRefresh = async () => {
        refused = refuse("renameSync", lockDir, 2, code);
      };
      try {
        expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
      } finally {
        vi.restoreAllMocks();
      }
      expect(refused).toEqual([code, code]);
      expect(existsSync(lockDir())).toBe(false);
      expect(leftovers()).toEqual([]);
    },
  );

  it("breaks a stale lock that is busy on a later try", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    const refused = refuse("renameSync", lockDir, 7, "EBUSY"); // more than one break retries
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(refused).toHaveLength(7);
    expect(leftovers()).toEqual([]);
  });

  it("raises on a stale lock still busy past twice the stale age", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", 2 * LOCK_STALE_MS + 1000);
    refuse("renameSync", lockDir, Number.POSITIVE_INFINITY, "EBUSY");
    try {
      await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(
        /is stale but cannot be broken \(EBUSY\)/,
      );
    } finally {
      vi.restoreAllMocks();
    }
    expect(existsSync(lockDir())).toBe(true);
  });

  it("waits a lock's rename and the release's claim for as long as the Python SDK does", () => {
    expect([RELEASE_WAIT_MS, RENAME_TRIES, RENAME_WAIT_MS]).toEqual([2000, 5, 20]);
  });

  /** Refuse the next `times` renames onto the lock with `code`, as Windows does while tmp is open. */
  function refuseTaking(times: number, code: string): string[] {
    const refused: string[] = [];
    const real = fs.renameSync;
    vi.spyOn(fs, "renameSync").mockImplementation(((from: string, to: string) => {
      if (to === lockDir() && refused.length < times) {
        refused.push(code);
        throw Object.assign(new Error(code), { code });
      }
      real(from, to);
    }) as never);
    return refused;
  }

  it.each(["EACCES", "EPERM", "EBUSY"])("takes a lock that is not there once %s refusals stop", async (code) => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const refused = refuseTaking(RENAME_TRIES - 1, code);
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(refused).toHaveLength(RENAME_TRIES - 1);
    expect(existsSync(lockDir())).toBe(false);
    expect(leftovers()).toEqual([]);
  });

  it("raises on a lock that is not there and is refused every try, never waits for it", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const refused = refuseTaking(Number.POSITIVE_INFINITY, "EACCES");
    try {
      const taking = accessToken(undefined, { fetch: srv.fetch });
      await expect(taking).rejects.toThrow(LoomError);
      await expect(taking).rejects.toThrow(`cannot take the credentials lock at ${lockDir()} (EACCES)`);
    } finally {
      vi.restoreAllMocks();
    }
    expect(refused).toHaveLength(RENAME_TRIES);
    expect(srv.sent).toHaveLength(0);
    expect(leftovers()).toEqual([]);
  });

  it("releases under a breaker's claim, waiting for it to go", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    let refused: string[] = [];
    srv.onRefresh = async () => {
      refused = refuse("mkdirSync", () => `${lockDir()}.breaking`, 3, "EEXIST"); // a waiter breaking it
    };
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(refused).toHaveLength(3);
    expect(existsSync(lockDir())).toBe(false);
    expect(existsSync(`${lockDir()}.breaking`)).toBe(false);
  });

  it("waits for a breaker's claim on a timer, so the event loop goes on meanwhile", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const claim = `${lockDir()}.breaking`;
    let fired = false;
    let firedBeforeClaim = false;
    srv.onRefresh = async () => {
      let refusals = 0;
      const real = fs.mkdirSync;
      vi.spyOn(fs, "mkdirSync").mockImplementation(((p: string, ...rest: unknown[]) => {
        if (p === claim && refusals < 3) {
          if (refusals++ === 0) setTimeout(() => (fired = true), 0);
          throw Object.assign(new Error("EEXIST"), { code: "EEXIST" }); // a waiter breaking it
        }
        if (p === claim) firedBeforeClaim = fired;
        return (real as (...a: unknown[]) => unknown)(p, ...rest);
      }) as never);
    };
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(firedBeforeClaim).toBe(true);
    expect(existsSync(lockDir())).toBe(false);
  });

  it("dates a release's claim by this machine's clock", async () => {
    // A file server whose clock lags would otherwise make a fresh claim look old to a breaker.
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const claim = `${lockDir()}.breaking`;
    let claimAge = Number.NaN;
    srv.onRefresh = async () => {
      const mkdir = fs.mkdirSync as (...a: unknown[]) => unknown;
      const rmdir = fs.rmdirSync as (...a: unknown[]) => unknown;
      vi.spyOn(fs, "mkdirSync").mockImplementation(((p: string, ...rest: unknown[]) => {
        const made = mkdir(p, ...rest);
        if (p === claim) age(claim, 3_600_000); // dated by a file server an hour behind
        return made;
      }) as never);
      vi.spyOn(fs, "rmdirSync").mockImplementation(((p: string, ...rest: unknown[]) => {
        if (p === claim) claimAge = Date.now() - statSync(claim).mtimeMs;
        return rmdir(p, ...rest);
      }) as never);
    };
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(claimAge).toBeLessThan(5000);
  });

  it("leaves its lock when a breaker's claim does not go in time", async () => {
    // Renaming it then could rename away a lock a third process takes after it.
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    let owner = "";
    srv.onRefresh = async () => {
      owner = readFileSync(join(lockDir(), "owner"), "utf8");
      mkdirSync(`${lockDir()}.breaking`);
    };
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    expect(readFileSync(join(lockDir(), "owner"), "utf8")).toBe(owner);
  });

  it("sweeps what a process that died taking or breaking the lock left beside it", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const loom = join(home, ".loom");
    for (const name of [".credentials.lock.d.0a.tmp", ".credentials.lock.d.0b.stale", ".credentials.lock.d.0c.tmp"]) {
      mkdirSync(join(loom, name));
      writeFileSync(join(loom, name, "owner"), "{}");
    }
    age(join(loom, ".credentials.lock.d.0a.tmp"), LOCK_STALE_MS + 1000);
    age(join(loom, ".credentials.lock.d.0b.stale"), LOCK_STALE_MS + 1000);
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    expect(leftovers()).toEqual([".credentials.lock.d.0c.tmp"]); // another process may be taking it now
  });

  it.each([
    ["claiming it", "mkdirSync", () => `${lockDir()}.breaking`],
    ["renaming it away", "renameSync", lockDir],
  ] as const)("raises, never spins, on a stale lock it cannot break: %s fails", async (what, call, target) => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    heldBy(1, "another-machine", LOCK_STALE_MS + 1000);
    const code = what === "claiming it" ? "EACCES" : "EROFS";
    refuse(call, target, Number.POSITIVE_INFINITY, code);
    try {
      await expect(accessToken(undefined, { fetch: srv.fetch })).rejects.toThrow(
        new RegExp(`credentials lock at .* is stale but cannot be broken \\(${code}\\)`),
      );
    } finally {
      vi.restoreAllMocks();
    }
    expect(srv.sent).toHaveLength(0);
    expect(existsSync(lockDir())).toBe(true);
  });

  it("keeps a refresh that succeeded when deleting the released lock fails", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    vi.spyOn(fs, "rmSync").mockImplementation(() => {
      throw Object.assign(new Error("resource busy"), { code: "EBUSY" });
    });
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(existsSync(lockDir())).toBe(false); // renamed away; the sweep deletes the rest
  });

  it("dates the lock by this machine's clock when it takes it", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const touched: string[] = [];
    const real = fs.utimesSync;
    vi.spyOn(fs, "utimesSync").mockImplementation(((path: string, a: Date, m: Date) => {
      touched.push(path);
      return real(path, a, m);
    }) as never);
    try {
      await accessToken(undefined, { fetch: srv.fetch });
    } finally {
      vi.restoreAllMocks();
    }
    // The private directory, before it is renamed onto the lock (a file server's clock otherwise),
    // then the release's claim.
    expect(touched.map((p) => p.slice(lockDir().length))).toEqual([expect.stringMatching(/\.tmp$/), ".breaking"]);
  });

  it("touches the lock every heartbeat while it holds it", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    let touched = Number.NaN;
    srv.onRefresh = async () => {
      const old = new Date(Date.now() - 3_600_000);
      utimesSync(lockDir(), old, old);
      vi.advanceTimersByTime(LOCK_HEARTBEAT_MS);
      touched = Date.now() - statSync(lockDir()).mtimeMs;
    };
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      await accessToken(undefined, { fetch: srv.fetch });
    } finally {
      vi.useRealTimers();
    }
    expect(touched).toBeLessThan(5000);
  });

  it("releases the lock when the process exits holding it, and unhooks after", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const before = ["exit", "SIGINT", "SIGTERM"].map((e) => process.listenerCount(e));
    let during: number[] = [];
    srv.onRefresh = async () => {
      during = ["exit", "SIGINT", "SIGTERM"].map((e) => process.listenerCount(e));
      const exit = process.listeners("exit").at(-1) as () => void;
      exit();
      expect(() => statSync(lockDir())).toThrow();
    };
    expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    expect(during).toEqual(before.map((c) => c + 1));
    expect(["exit", "SIGINT", "SIGTERM"].map((e) => process.listenerCount(e))).toEqual(before);
  });

  it("finishes its own release when the process exits while the release waits", async () => {
    // The release holds its claim and waits out a busy rename (Windows) when the exit comes:
    // a second release would find that claim taken and leave the lock.
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    const before = process.listenerCount("exit");
    let exited = false;
    let gone = false; // what the process leaves when it ends as the hook returns
    srv.onRefresh = async () => {
      const real = fs.renameSync;
      vi.spyOn(fs, "renameSync").mockImplementation(((from: string, ...rest: unknown[]) => {
        if (from === lockDir() && !exited) {
          exited = true;
          const exit = process.listeners("exit").at(-1) as () => void;
          // The exit lands while the release waits to try again; the process ends as it returns.
          setImmediate(() => {
            exit();
            gone = !existsSync(lockDir()) && !existsSync(`${lockDir()}.breaking`);
          });
          throw Object.assign(new Error("EBUSY"), { code: "EBUSY" });
        }
        return (real as (...a: unknown[]) => unknown)(from, ...rest);
      }) as never);
    };
    const started = performance.now();
    try {
      expect(await accessToken(undefined, { fetch: srv.fetch })).toBe("a1");
    } finally {
      vi.restoreAllMocks();
    }
    expect(exited).toBe(true);
    expect(gone).toBe(true);
    expect(() => statSync(lockDir())).toThrow();
    expect(() => statSync(`${lockDir()}.breaking`)).toThrow();
    expect(performance.now() - started).toBeLessThan(RELEASE_WAIT_MS);
    expect(process.listenerCount("exit")).toBe(before);
  });

  it("leaves a lock another process took over while it held it", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    srv.onRefresh = async () => {
      writeFileSync(join(lockDir(), "owner"), '{"pid": 1}'); // broken as stale and taken meanwhile
    };
    await accessToken(undefined, { fetch: srv.fetch });
    expect(readFileSync(join(lockDir(), "owner"), "utf8")).toBe('{"pid": 1}');
  });

  it("ends a refresh within its deadline: a connect failure past it is not retried", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    let skew = 0;
    const real = performance.now.bind(performance);
    vi.spyOn(performance, "now").mockImplementation(() => real() + skew);
    srv.queue("/oauth/token", Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } }));
    const fetch = async (url: string, init: RequestInit) => {
      if (new URL(url).pathname === "/oauth/token") skew += 90_000;
      return srv.fetch(url, init);
    };
    try {
      await expect(accessToken(undefined, { fetch })).rejects.toThrow(/took over 90 s; retry/);
    } finally {
      vi.restoreAllMocks();
    }
    expect(srv.sent.filter((c) => c.path === "/oauth/token")).toHaveLength(1);
  });

  it("names its holder in the owner file while held", async () => {
    const srv = fakeServer();
    store("default", { expires_at: now() - 1 });
    let owner: Record<string, unknown> = {};
    srv.onRefresh = async () => {
      owner = JSON.parse(readFileSync(join(lockDir(), "owner"), "utf8"));
    };
    await accessToken(undefined, { fetch: srv.fetch });
    expect(owner).toMatchObject({ pid: process.pid });
    expect(typeof owner.host).toBe("string");
    expect(typeof owner.time).toBe("number");
  });
});

describe("logout and whoami", () => {
  it("revokes the refresh token at the profile's server and forgets the profile", async () => {
    const srv = fakeServer();
    store("default");
    store("keep");
    expect(await logout(undefined, { fetch: srv.fetch })).toBe(true);
    expect(srv.sent.find((c) => c.path === "/oauth/revoke")?.form.get("token")).toBe("r0");
    expect(Object.keys(profiles())).toEqual(["keep"]);
    expect(await logout("nobody", { fetch: srv.fetch })).toBe(false);
  });

  it("forgets the profile even when the server cannot be reached", async () => {
    store("default");
    const down = async () => {
      throw new TypeError("fetch failed");
    };
    expect(await logout(undefined, { fetch: down })).toBe(false);
    expect(profiles()).toEqual({});
  });

  it("answers who the profile's token says you are", async () => {
    const srv = fakeServer();
    store("work");
    const opts: CredentialOptions = { fetch: srv.fetch };
    expect(await whoami("work", opts)).toEqual({
      profile: "work",
      server: SERVER,
      subject: "ada",
      org: "acme",
    });
  });
});

describe("StoredLogin through the transport", () => {
  it("refreshes once on a 401 and resends with the new token", async () => {
    const srv = fakeServer();
    store("default");
    srv.valid = "a1";
    const t = new Transport({
      baseUrl: SERVER,
      tokenProvider: new StoredLogin("default", SERVER, srv),
      fetch: srv.fetch,
    });
    expect(await t.post("memory.get", {})).toEqual({ found: true });
    const ops = srv.sent.filter((c) => c.path === "/v1/memory.get").map((c) => c.auth);
    expect(ops).toEqual(["Bearer a0", "Bearer a1"]);
    expect(t.apiKeyHint).toBe("login:default");
    expect(JSON.stringify(t)).not.toContain("a1");
  });

  it("never sends the profile's token to another origin", async () => {
    const srv = fakeServer();
    store("default");
    const t = new Transport({
      baseUrl: "https://other.example",
      tokenProvider: new StoredLogin("default", SERVER, srv),
      fetch: srv.fetch,
    });
    await expect(t.post("memory.get", {})).rejects.toThrow(/refusing to send login:default's token/);
    expect(srv.sent).toHaveLength(0);
  });
});

describe("defaultProfile", () => {
  it("is LOOM_PROFILE, else default", () => {
    expect(defaultProfile()).toBe("default");
    vi.stubEnv("LOOM_PROFILE", " work ");
    expect(defaultProfile()).toBe("work");
  });
});

describe("sameServer", () => {
  it.each([
    ["https://loom.example", "https://LOOM.example:443/", true],
    ["http://loom.example:80/x/", "http://loom.example/x", true],
    ["https://loom.example", "http://loom.example", false],
    ["https://loom.example/a", "https://loom.example/b", false],
    ["https://loom.example:8443", "https://loom.example", false],
    ["not a url", "https://loom.example", false],
  ])("%s vs %s is %s", (a, b, same) => {
    expect(sameServer(a, b)).toBe(same);
  });
});

describe("the credential a client picks", () => {
  /** The Authorization header of the first op a client built from `opts` sends. */
  async function sentAuth(opts: Parameters<typeof readEnv>[0], srv = fakeServer()): Promise<string | null> {
    const t = new Transport({ ...readEnv({ ...opts, fetch: srv.fetch }), fetch: srv.fetch });
    await t.post("memory.get", {});
    return srv.sent.find((c) => c.path === "/v1/memory.get")?.auth ?? null;
  }

  function writeToken(text: string): void {
    mkdirSync(join(home, ".loom"), { recursive: true });
    writeFileSync(join(home, ".loom", "token"), text);
  }

  it("takes apiKey, then LOOM_API_KEY, then a bot, then the stored login, then ~/.loom/token", async () => {
    store("default");
    writeToken("  file-token \nsecond line\n");
    vi.stubEnv("LOOM_API_KEY", "env-key");
    expect(await sentAuth({ baseUrl: SERVER, apiKey: "explicit" })).toBe("Bearer explicit");
    expect(await sentAuth({ baseUrl: SERVER })).toBe("Bearer env-key");
    vi.stubEnv("LOOM_API_KEY", "");
    vi.stubEnv("LOOM_CLIENT_ID", "bot-1");
    vi.stubEnv("LOOM_WORKLOAD_TOKEN", "wl");
    expect(readEnv({ baseUrl: SERVER }).tokenProvider).toBeInstanceOf(MachineLogin);
    vi.stubEnv("LOOM_CLIENT_ID", "");
    expect(await sentAuth({ baseUrl: SERVER })).toBe("Bearer a0");
    expect(await sentAuth({ baseUrl: "https://other.example" })).toBe("Bearer file-token");
    await rm(join(home, ".loom", "token"));
    expect(await sentAuth({ baseUrl: "https://other.example" })).toBeNull();
  });

  it("uses LOOM_PROFILE's stored login, matched to the server as a browser compares origins", async () => {
    store("work", { access_token: "work-token", server: "https://loom.example:443" });
    vi.stubEnv("LOOM_PROFILE", "work");
    vi.stubEnv("LOOM_BASE_URL", "https://LOOM.example/");
    expect(await sentAuth({})).toBe("Bearer work-token");
  });

  it("decides on the first request, not in the constructor", async () => {
    const srv = fakeServer();
    const opts = readEnv({ baseUrl: SERVER, fetch: srv.fetch });
    expect(opts.tokenProvider).toBeInstanceOf(LocalCredential);
    store("default"); // signed in after the client was made
    const t = new Transport({ ...opts, fetch: srv.fetch });
    await t.post("memory.get", {});
    expect(srv.sent[0].auth).toBe("Bearer a0");
    expect(t.apiKeyHint).toBe("login:default");
  });

  it("sends no header and no second request on a 401 when there is no credential", async () => {
    const srv = fakeServer();
    srv.valid = "x";
    const t = new Transport({ ...readEnv({ baseUrl: SERVER, fetch: srv.fetch }), fetch: srv.fetch });
    await expect(t.post("memory.get", {})).rejects.toThrow(AuthError);
    expect(srv.sent.map((c) => c.auth)).toEqual([null]);
    expect(t.apiKeyHint).toBe("none");
  });

  it("raises a 401 on the ~/.loom/token credential instead of resending the same token", async () => {
    writeToken("file-token");
    const srv = fakeServer();
    srv.valid = "another";
    const t = new Transport({ ...readEnv({ baseUrl: SERVER, fetch: srv.fetch }), fetch: srv.fetch });
    await expect(t.post("memory.get", {})).rejects.toMatchObject({ status: 401 });
    expect(srv.sent.map((c) => c.auth)).toEqual(["Bearer file-token"]);
  });

  it("never looks for a stored login or token file in a browser", () => {
    store("default");
    writeToken("file-token");
    vi.stubGlobal("window", {});
    vi.stubGlobal("document", {});
    expect(readEnv({ baseUrl: SERVER }).tokenProvider).toBeUndefined();
    expect(() => readEnv({ profile: "default" })).toThrow(/needs Node/);
  });

  it("with `profile`, uses that session on its own server and refuses a second credential", async () => {
    store("staging", { server: "https://staging.example", access_token: "stage-token" });
    const opts = readEnv({ profile: "staging" });
    expect(opts.baseUrl).toBe("https://staging.example");
    expect(opts.tokenProvider).toBeInstanceOf(StoredLogin);
    expect(readEnv({ profile: "staging", baseUrl: "https://staging.example/" }).baseUrl).toBe(
      "https://staging.example",
    );
    expect(() => readEnv({ profile: "staging", apiKey: "k" })).toThrow(/pass one or the other/);
    expect(() => readEnv({ profile: "staging", baseUrl: SERVER })).toThrow(/is a session for https:\/\/staging/);
    expect(() => readEnv({ profile: "nobody" })).toThrow(AuthError);
  });
});
