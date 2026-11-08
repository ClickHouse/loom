import { generateKeyPairSync, type KeyObject, verify } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readEnv } from "./client.js";
import { AuthError, BackendUnavailable, type LoomError, TimeoutError } from "./errors.js";
import {
  type KeySource,
  MachineLogin,
  type MachineLoginOptions,
  machineLogin,
  TOKEN_EXCHANGE,
  type TokenProvider,
} from "./machine.js";
import { type FetchLike, Transport } from "./transport.js";

const BASE = "http://loom.test";
const PUBLIC_ENDPOINT = "https://loom.example/oauth/token";
const ENV = [
  "LOOM_API_KEY",
  "LOOM_CLIENT_ID",
  "LOOM_CLIENT_KEY_FILE",
  "LOOM_CLIENT_KEY",
  "LOOM_CLIENT_KEY_SECRET",
  "LOOM_WORKLOAD_TOKEN",
  "LOOM_WORKLOAD_TOKEN_FILE",
  "LOOM_WORKLOAD_AUDIENCE",
  "ACTIONS_ID_TOKEN_REQUEST_URL",
  "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
];

beforeEach(() => {
  for (const name of ENV) vi.stubEnv(name, "");
});
afterEach(() => {
  vi.unstubAllEnvs();
});

function pem(type: "ec" | "ed25519" | "rsa"): { privatePem: string; publicKey: KeyObject } {
  const pair =
    type === "ec"
      ? generateKeyPairSync("ec", { namedCurve: "P-256" })
      : type === "ed25519"
        ? generateKeyPairSync("ed25519")
        : generateKeyPairSync("rsa", { modulusLength: 2048 });
  return { privatePem: pair.privateKey.export({ type: "pkcs8", format: "pem" }).toString(), publicKey: pair.publicKey };
}

/** A Loom access token: a JWT whose payload the SDK decodes for its claims. */
function accessToken(n: number): string {
  const part = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${part({ alg: "none" })}.${part({ sub: `bot-${n}`, org: "acme", namespaces: ["ci"], roles: ["writer"] })}.s`;
}

type Answer = { status: number; body?: unknown };

/** Fakes the server's login endpoints and its `/v1` ops; every request is recorded. */
function server(opts: { token?: Answer[]; v1?: Answer[]; expiresIn?: number } = {}) {
  const calls: Array<{ url: string; init: RequestInit; form?: URLSearchParams }> = [];
  let issued = 0;
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    // A copy: the transport reuses its headers object across attempts.
    const headers = { ...(init.headers as Record<string, string>) };
    const call: { url: string; init: RequestInit; form?: URLSearchParams } = { url, init: { ...init, headers } };
    calls.push(call);
    const json = (a: Answer) => new Response(JSON.stringify(a.body ?? {}), { status: a.status });
    if (url === `${BASE}/.well-known/oauth-authorization-server`)
      return json({ status: 200, body: { token_endpoint: PUBLIC_ENDPOINT } });
    if (url === `${BASE}/oauth/token`) {
      call.form = new URLSearchParams(String(init.body));
      const scripted = opts.token?.shift();
      if (scripted) return json(scripted);
      issued++;
      return json({ status: 200, body: { access_token: accessToken(issued), expires_in: opts.expiresIn ?? 3600 } });
    }
    if (url.startsWith(`${BASE}/v1/`)) return json(opts.v1?.shift() ?? { status: 200, body: { ok: true } });
    if (url.startsWith("https://actions.example/")) return json({ status: 200, body: { value: "gha-oidc-token" } });
    throw new Error(`unexpected ${url}`);
  };
  const tokenCalls = () => calls.filter((c) => c.url === `${BASE}/oauth/token`);
  return { fetch, calls, tokenCalls };
}

function clock(start = 1_700_000_000_000) {
  const c = { t: start, now: () => c.t };
  return c;
}

function verifyAssertion(
  assertion: string,
  publicKey: KeyObject,
): { header: Record<string, unknown>; claims: Record<string, unknown> } {
  const [h, p, s] = assertion.split(".");
  const header = JSON.parse(Buffer.from(h, "base64url").toString()) as Record<string, unknown>;
  const data = Buffer.from(`${h}.${p}`);
  const sig = Buffer.from(s, "base64url");
  const ok =
    header.alg === "ES256"
      ? verify("sha256", data, { key: publicKey, dsaEncoding: "ieee-p1363" }, sig)
      : verify(header.alg === "EdDSA" ? null : "sha256", data, publicKey, sig);
  expect(ok).toBe(true);
  return { header, claims: JSON.parse(Buffer.from(p, "base64url").toString()) as Record<string, unknown> };
}

describe("MachineLogin with a key", () => {
  it.each([
    ["ec", "ES256"],
    ["ed25519", "EdDSA"],
    ["rsa", "RS256"],
  ] as const)("signs a %s key's assertion as %s, for the token endpoint the server names", async (type, alg) => {
    const { privatePem, publicKey } = pem(type);
    const srv = server();
    const c = clock();
    const bot = new MachineLogin({
      baseUrl: `${BASE}/`,
      clientId: "ci-bot",
      key: { source: "LOOM_CLIENT_KEY", value: privatePem },
      fetch: srv.fetch,
      now: c.now,
    });
    expect(await bot.token()).toBe(accessToken(1));
    const form = srv.tokenCalls()[0].form as URLSearchParams;
    expect(form.get("grant_type")).toBe("client_credentials");
    expect(form.get("client_id")).toBe("ci-bot");
    expect(form.get("client_assertion_type")).toBe("urn:ietf:params:oauth:client-assertion-type:jwt-bearer");
    const { header, claims } = verifyAssertion(form.get("client_assertion") ?? "", publicKey);
    expect(header.alg).toBe(alg);
    expect(claims).toMatchObject({ iss: "ci-bot", sub: "ci-bot", aud: PUBLIC_ENDPOINT, iat: c.t / 1000 });
    expect(claims.exp).toBe((claims.iat as number) + 120);
    expect(claims.jti).toMatch(/^[0-9a-f]{32}$/);
  });

  it("caches the token, renews it 5 minutes before expiry, and reads the metadata once", async () => {
    const srv = server();
    const c = clock();
    const bot = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY", value: pem("ec").privatePem },
      fetch: srv.fetch,
      now: c.now,
    });
    expect(bot.current()).toBeUndefined();
    const [a, b] = await Promise.all([bot.token(), bot.token()]);
    expect(a).toBe(b);
    expect(bot.current()).toBe(a);
    c.t += 3300_000; // exactly at the renewal point: still good
    expect(await bot.token()).toBe(a);
    c.t += 1;
    expect(await bot.token()).toBe(accessToken(2));
    expect(srv.tokenCalls()).toHaveLength(2);
    expect(srv.calls.filter((x) => x.url.includes("well-known"))).toHaveLength(1);
    expect(JSON.stringify(bot)).not.toContain(a);
  });

  it("renews a short-lived token halfway through its life", async () => {
    const srv = server({ expiresIn: 120 });
    const c = clock();
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", fetch: srv.fetch, now: c.now });
    vi.stubEnv("LOOM_WORKLOAD_TOKEN", "wl");
    await bot.token();
    c.t += 60_001;
    await bot.token();
    expect(srv.tokenCalls()).toHaveLength(2);
  });

  it("keeps the token in hand when renewal fails, retries after 30 s, and fails once it expires", async () => {
    const script: Answer[] = [];
    const srv = server({ token: script });
    const c = clock();
    const bot = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY", value: pem("ec").privatePem },
      fetch: srv.fetch,
      now: c.now,
    });
    const held = await bot.token();
    script.push({ status: 503 }, { status: 503 }, { status: 503 });
    c.t += 3400_000;
    expect(await bot.token()).toBe(held); // 503: the token in hand
    c.t += 29_000;
    expect(await bot.token()).toBe(held); // within 30 s: no new try
    expect(srv.tokenCalls()).toHaveLength(2);
    c.t += 2_000;
    expect(await bot.token()).toBe(held); // tried again, failed again
    expect(srv.tokenCalls()).toHaveLength(3);
    c.t += 3600_000; // past expiry: the failure surfaces
    await expect(bot.token()).rejects.toBeInstanceOf(BackendUnavailable);
    expect(await bot.token()).toBe(accessToken(2)); // and Loom is back
  });

  it("maps the token endpoint's refusal to AuthError with its error and description, 429 to BackendUnavailable", async () => {
    const srv = server({
      token: [
        { status: 400, body: { error: "invalid_client", error_description: "unknown key" } },
        { status: 429 },
        { status: 200, body: { token_type: "Bearer" } },
      ],
    });
    const bot = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY", value: pem("ec").privatePem },
      fetch: srv.fetch,
    });
    const err = await bot.token().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(AuthError);
    expect((err as AuthError).message).toBe("bot b: invalid_client unknown key");
    expect((err as AuthError).code).toBe("invalid_client");
    expect((err as AuthError).detail).toMatchObject({ error: "invalid_client", error_description: "unknown key" });
    await expect(bot.token()).rejects.toBeInstanceOf(BackendUnavailable);
    await expect(bot.token()).rejects.toBeInstanceOf(AuthError);
  });
});

describe("answers other than the token's", () => {
  const busyOrRefused = [
    [404, AuthError],
    [503, BackendUnavailable],
  ] as const;
  /** A 200 whose body is a proxy's page, JSON null, or cut off mid-read. */
  const bodies = [
    ["an HTML page", () => new Response("<html>sign in</html>", { status: 200 }), AuthError],
    ["JSON null", () => new Response("null", { status: 200 }), AuthError],
    [
      "a body cut off in transit",
      () =>
        new Response(
          new ReadableStream({
            start(c) {
              c.error(new TypeError("terminated"));
            },
          }),
          { status: 200 },
        ),
      BackendUnavailable,
    ],
  ] as const;

  it.each(bodies)("reads server metadata answering %s as a Loom error", async (_, body, error) => {
    const answer = async (url: string) =>
      url.endsWith("/.well-known/oauth-authorization-server")
        ? body()
        : new Response(JSON.stringify({ access_token: accessToken(1) }), { status: 200 });
    const key = { source: "LOOM_CLIENT_KEY" as const, value: pem("ec").privatePem };
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", key, fetch: answer });
    await expect(bot.token()).rejects.toBeInstanceOf(error);
  });

  it.each(bodies)("reads the token endpoint answering %s as a Loom error", async (_, body, error) => {
    const answer = async (url: string) =>
      url.endsWith("/.well-known/oauth-authorization-server")
        ? new Response(JSON.stringify({ token_endpoint: PUBLIC_ENDPOINT }), { status: 200 })
        : body();
    const key = { source: "LOOM_CLIENT_KEY" as const, value: pem("ec").privatePem };
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", key, fetch: answer });
    await expect(bot.token()).rejects.toBeInstanceOf(error);
  });

  it.each(bodies)("reads GitHub Actions answering %s as a Loom error", async (_, body, error) => {
    vi.stubEnv("LOOM_CLIENT_ID", "gha");
    vi.stubEnv("LOOM_WORKLOAD_AUDIENCE", "https://loom.example");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/token");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-bearer");
    const bot = machineLogin(BASE, { fetch: async () => body() }) as MachineLogin;
    await expect(bot.token()).rejects.toBeInstanceOf(error);
  });

  it.each([
    [503, "dependency"],
    [429, "capacity"],
  ] as const)("classes server metadata answering %s retryable", async (status, category) => {
    const answer = async (url: string) =>
      url.endsWith("/.well-known/oauth-authorization-server")
        ? new Response("{}", { status })
        : new Response(JSON.stringify({ access_token: accessToken(1) }), { status: 200 });
    const key = { source: "LOOM_CLIENT_KEY" as const, value: pem("ec").privatePem };
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", key, fetch: answer });
    const err = (await bot.token().catch((e: unknown) => e)) as LoomError;
    expect([err.category, err.retryable]).toEqual([category, true]);
  });

  it.each(busyOrRefused)("reads server metadata answering %s as the Python SDK does", async (status, error) => {
    const answer = async (url: string) =>
      url.endsWith("/.well-known/oauth-authorization-server")
        ? new Response("{}", { status })
        : new Response(JSON.stringify({ access_token: accessToken(1) }), { status: 200 });
    const key = { source: "LOOM_CLIENT_KEY" as const, value: pem("ec").privatePem };
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", key, fetch: answer });
    await expect(bot.token()).rejects.toBeInstanceOf(error);
  });

  it.each(busyOrRefused)("reads GitHub Actions answering %s as the Python SDK does", async (status, error) => {
    vi.stubEnv("LOOM_CLIENT_ID", "gha");
    vi.stubEnv("LOOM_WORKLOAD_AUDIENCE", "https://loom.example");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/token");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-bearer");
    const asked: string[] = [];
    const answer = async (url: string) => {
      asked.push(url);
      return new Response("{}", { status });
    };
    const bot = machineLogin(BASE, { fetch: answer }) as MachineLogin;
    await expect(bot.token()).rejects.toBeInstanceOf(error);
    expect(asked).toHaveLength(1);
    expect(asked[0]).toMatch(/^https:\/\/actions\.example\/token\?audience=/);
  });
});

describe("key sources", () => {
  it("names the source of a key that is not a PEM", async () => {
    // As `docker --env-file` leaves it: the newlines written as `\n`.
    const broken = pem("ec").privatePem.replaceAll("\n", "\\n");
    const srv = server();
    const source: KeySource = "LOOM_CLIENT_KEY";
    const opts: MachineLoginOptions = {
      baseUrl: BASE,
      clientId: "b",
      key: { source, value: broken },
      fetch: srv.fetch,
    };
    const bot = new MachineLogin(opts);
    await expect(bot.token()).rejects.toThrow(new AuthError("LOOM_CLIENT_KEY: not a PEM private key"));
  });

  it("refuses a secret stored as binary as a configuration error, not a retry", async () => {
    const fakeModule = {
      GetSecretValueCommand: class {
        constructor(readonly input: { SecretId: string }) {}
      },
      SecretsManagerClient: class {
        async send() {
          return { SecretBinary: new Uint8Array([1]) };
        }
      },
    };
    const srv = server();
    const bot = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY_SECRET", value: "loom/bot" },
      fetch: srv.fetch,
      importModule: async (name) => (name === "@aws-sdk/client-secrets-manager" ? fakeModule : import(name)),
    });
    await expect(bot.token()).rejects.toBeInstanceOf(AuthError);
  });

  it("re-reads LOOM_CLIENT_KEY_FILE for each token, so a rotated key is picked up", async () => {
    const dir = mkdtempSync(join(tmpdir(), "loom-key-"));
    const file = join(dir, "bot.pem");
    const one = pem("ec");
    const two = pem("ec");
    writeFileSync(file, one.privatePem);
    vi.stubEnv("LOOM_CLIENT_ID", "file-bot");
    vi.stubEnv("LOOM_CLIENT_KEY_FILE", file);
    const srv = server();
    const c = clock();
    const bot = machineLogin(BASE, { fetch: srv.fetch, now: c.now }) as MachineLogin;
    await bot.token();
    writeFileSync(file, two.privatePem);
    c.t += 3600_000;
    await bot.token();
    const [a, b] = srv.tokenCalls().map((x) => x.form?.get("client_assertion") ?? "");
    verifyAssertion(a, one.publicKey);
    verifyAssertion(b, two.publicKey);
  });

  it("refuses more than one key source, naming them", () => {
    vi.stubEnv("LOOM_CLIENT_ID", "b");
    vi.stubEnv("LOOM_CLIENT_KEY", "pem");
    vi.stubEnv("LOOM_CLIENT_KEY_SECRET", "loom/bot");
    expect(() => machineLogin(BASE)).toThrow(/LOOM_CLIENT_KEY, LOOM_CLIENT_KEY_SECRET/);
    expect(() => machineLogin(BASE)).toThrow(AuthError);
  });

  it("is off without LOOM_CLIENT_ID", () => {
    vi.stubEnv("LOOM_CLIENT_KEY", "pem");
    expect(machineLogin(BASE)).toBeUndefined();
  });

  it("reads LOOM_CLIENT_KEY_SECRET from Secrets Manager on each token, in the ARN's region", async () => {
    const one = pem("ec");
    const two = pem("ec");
    const secrets = [one.privatePem, two.privatePem];
    const seen: Array<{ region?: string; secretId: string }> = [];
    const arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:loom/bot-AbCdEf";
    const fakeModule = {
      GetSecretValueCommand: class {
        constructor(readonly input: { SecretId: string }) {}
      },
      SecretsManagerClient: class {
        constructor(readonly config: { region?: string }) {}
        async send(cmd: { input: { SecretId: string } }) {
          seen.push({ region: this.config.region, secretId: cmd.input.SecretId });
          return { SecretString: secrets.shift() };
        }
      },
    };
    vi.stubEnv("LOOM_CLIENT_ID", "sm-bot");
    vi.stubEnv("LOOM_CLIENT_KEY_SECRET", arn);
    const srv = server();
    const c = clock();
    const bot = machineLogin(BASE, {
      fetch: srv.fetch,
      now: c.now,
      importModule: async (name) => (name === "@aws-sdk/client-secrets-manager" ? fakeModule : import(name)),
    }) as MachineLogin;
    await bot.token();
    c.t += 3600_000;
    await bot.token();
    expect(seen).toEqual([
      { region: "eu-west-1", secretId: arn },
      { region: "eu-west-1", secretId: arn },
    ]);
    const [a, b] = srv.tokenCalls().map((x) => x.form?.get("client_assertion") ?? "");
    verifyAssertion(a, one.publicKey);
    verifyAssertion(b, two.publicKey);
  });

  it("takes the default region for a bare secret id, and names the package when it is not installed", async () => {
    const regions: Array<string | undefined> = [];
    const fakeModule = {
      GetSecretValueCommand: class {},
      SecretsManagerClient: class {
        constructor(config: { region?: string }) {
          regions.push(config.region);
        }
        async send() {
          return { SecretString: pem("ec").privatePem };
        }
      },
    };
    const srv = server();
    const withSdk = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY_SECRET", value: "loom/bot" },
      fetch: srv.fetch,
      importModule: async (name) => (name.startsWith("@aws-sdk/") ? fakeModule : import(name)),
    });
    await withSdk.token();
    expect(regions).toEqual([undefined]);
    const without = new MachineLogin({
      baseUrl: BASE,
      clientId: "b",
      key: { source: "LOOM_CLIENT_KEY_SECRET", value: "loom/bot" },
      fetch: srv.fetch,
      importModule: async (name) => {
        if (name.startsWith("@aws-sdk/")) throw new Error(`Cannot find package '${name}'`);
        return import(name);
      },
    });
    await expect(without.token()).rejects.toThrow(/npm install @aws-sdk\/client-secrets-manager/);
  });
});

describe("MachineLogin with a workload token", () => {
  it("exchanges LOOM_WORKLOAD_TOKEN_FILE, re-read for each token", async () => {
    const file = join(mkdtempSync(join(tmpdir(), "loom-wl-")), "token");
    writeFileSync(file, "first\n");
    vi.stubEnv("LOOM_CLIENT_ID", "agent-7");
    vi.stubEnv("LOOM_WORKLOAD_TOKEN_FILE", file);
    const srv = server();
    const c = clock();
    const bot = machineLogin(BASE, { fetch: srv.fetch, now: c.now }) as MachineLogin;
    await bot.token();
    writeFileSync(file, "second\n");
    c.t += 3600_000;
    await bot.token();
    const forms = srv.tokenCalls().map((x) => x.form as URLSearchParams);
    expect(forms.map((f) => f.get("subject_token"))).toEqual(["first", "second"]);
    expect(forms[0].get("grant_type")).toBe(TOKEN_EXCHANGE);
    expect(forms[0].get("client_id")).toBe("agent-7");
    expect(forms[0].get("subject_token_type")).toBe("urn:ietf:params:oauth:token-type:jwt");
    expect(srv.calls.some((x) => x.url.includes("well-known"))).toBe(false);
  });

  it("asks GitHub Actions for a token for LOOM_WORKLOAD_AUDIENCE, keeping the runner's own query", async () => {
    vi.stubEnv("LOOM_CLIENT_ID", "gha");
    vi.stubEnv("LOOM_WORKLOAD_AUDIENCE", "https://loom.example");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/token?api-version=2.0");
    vi.stubEnv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-bearer");
    const srv = server();
    await (machineLogin(BASE, { fetch: srv.fetch }) as MachineLogin).token();
    const ask = srv.calls.find((x) => x.url.startsWith("https://actions.example/")) as (typeof srv.calls)[number];
    const url = new URL(ask.url);
    expect(url.searchParams.get("api-version")).toBe("2.0");
    expect(url.searchParams.get("audience")).toBe("https://loom.example");
    expect((ask.init.headers as Record<string, string>).Authorization).toBe("Bearer runner-bearer");
    expect(srv.tokenCalls()[0].form?.get("subject_token")).toBe("gha-oidc-token");
  });

  it("names every source when there is no key and no workload token", async () => {
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", fetch: server().fetch });
    await expect(bot.token()).rejects.toThrow(/LOOM_CLIENT_KEY_FILE.*LOOM_WORKLOAD_TOKEN_FILE/);
  });
});

describe("redirects", () => {
  it.each([307, 308])("refuses a %i from /oauth/token instead of sending the proof on", async (status) => {
    const sent: string[] = [];
    const fetch = async (url: string, init: RequestInit): Promise<Response> => {
      sent.push(url);
      expect(init.redirect).toBe("manual");
      return new Response(null, { status, headers: { Location: "http://elsewhere.test/collect" } });
    };
    vi.stubEnv("LOOM_WORKLOAD_TOKEN", "workload-proof");
    const bot = new MachineLogin({ baseUrl: BASE, clientId: "b", fetch });
    await expect(bot.token()).rejects.toBeInstanceOf(AuthError);
    expect(sent).toEqual([`${BASE}/oauth/token`]); // never the redirect's target
  });
});

describe("Transport with a token provider", () => {
  function bot(srv: ReturnType<typeof server>, baseUrl = BASE) {
    vi.stubEnv("LOOM_WORKLOAD_TOKEN", "wl");
    return new MachineLogin({ baseUrl, clientId: "b", fetch: srv.fetch });
  }
  const v1 = (srv: ReturnType<typeof server>) => srv.calls.filter((c) => c.url.startsWith(`${BASE}/v1/`));
  const auth = (c: { init: RequestInit }) => (c.init.headers as Record<string, string>).Authorization;

  it("asks again for a token the endpoint could not give, busy or away, and then sends the call", async () => {
    for (const status of [503, 500]) {
      const srv = server({ token: [{ status, body: {} }] });
      const t = new Transport({ baseUrl: BASE, tokenProvider: bot(srv), fetch: srv.fetch, sleep: async () => {} });
      expect(await t.post("memory.set", { org: "o" })).toEqual({ ok: true });
      expect(srv.tokenCalls()).toHaveLength(2);
    }
  });

  it("sends the bot's token and retries a 401 once with a fresh one", async () => {
    const srv = server({ v1: [{ status: 401, body: { detail: { code: "UNAUTHENTICATED" } } }] });
    const t = new Transport({ baseUrl: BASE, tokenProvider: bot(srv), fetch: srv.fetch, retry: { maxAttempts: 1 } });
    expect(await t.post("memory.set", { org: "o" })).toEqual({ ok: true });
    expect(v1(srv).map(auth)).toEqual([`Bearer ${accessToken(1)}`, `Bearer ${accessToken(2)}`]);
    expect(srv.tokenCalls()).toHaveLength(2);
  });

  it("counts the fresh-token resend as the same attempt, not a retry", async () => {
    const srv = server({ v1: [{ status: 401, body: { detail: { code: "UNAUTHENTICATED" } } }] });
    const t = new Transport({ baseUrl: BASE, tokenProvider: bot(srv), fetch: srv.fetch, retry: { maxAttempts: 1 } });
    await t.post("memory.set", { org: "o" });
    const count = (c: { init: RequestInit }) => (c.init.headers as Record<string, string>)["X-Loom-Retry-Count"];
    expect(v1(srv).map(count)).toEqual(["0", "0"]);
  });

  it("retries a call whose token endpoint was away, as a connection failure", async () => {
    let asked = 0;
    const provider: TokenProvider = {
      token: () => {
        asked++;
        if (asked === 1) throw new BackendUnavailable("token endpoint answered 503");
        return "t";
      },
      origin: BASE,
    };
    const srv = server();
    const t = new Transport({
      baseUrl: BASE,
      tokenProvider: provider,
      fetch: srv.fetch,
      retry: { maxAttempts: 2, backoffMs: 1, backoffMaxMs: 1 },
    });
    expect(await t.post("memory.get", { org: "o" })).toEqual({ ok: true });
    expect([asked, v1(srv).map(auth)]).toEqual([2, ["Bearer t"]]);
    const count = (c: { init: RequestInit }) => (c.init.headers as Record<string, string>)["X-Loom-Retry-Count"];
    expect(v1(srv).map(count)).toEqual(["0"]); // the server saw it once
  });

  it("takes a provider that answers a plain string, with or without a signal", async () => {
    const provider: TokenProvider = { token: () => "plain", origin: BASE };
    const srv = server();
    const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: srv.fetch });
    await t.post("memory.get", { org: "o" });
    await t.post("memory.get", { org: "o" }, { signal: new AbortController().signal });
    expect(v1(srv).map(auth)).toEqual(["Bearer plain", "Bearer plain"]);
  });

  it("asks the provider for nothing once the caller has cancelled", async () => {
    let asked = 0;
    const provider: TokenProvider = {
      token: async () => {
        asked++;
        return "t";
      },
      origin: BASE,
    };
    const srv = server();
    const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: srv.fetch });
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(t.post("memory.get", { org: "o" }, { signal: ctrl.signal })).rejects.toThrow("aborted");
    expect(asked).toBe(0);
    expect(v1(srv)).toHaveLength(0);
  });

  it("lets a caller give up on a stalled token while others keep waiting for it", async () => {
    let release: (t: string) => void = () => {};
    const pending = new Promise<string>((r) => {
      release = r;
    });
    const provider: TokenProvider = { token: () => pending, origin: BASE };
    const srv = server();
    const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: srv.fetch });
    const ctrl = new AbortController();
    const cancelled = t.post("memory.get", { org: "o" }, { signal: ctrl.signal });
    const patient = t.post("memory.get", { org: "o" });
    ctrl.abort();
    await expect(cancelled).rejects.toThrow("aborted");
    release("shared");
    expect(await patient).toEqual({ ok: true });
    expect(v1(srv).map(auth)).toEqual(["Bearer shared"]);
  });

  it("leaves no timer behind when a caller gives up on a stalled token", async () => {
    // A timer left running would keep a cancelled Node command alive until the timeout.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const provider: TokenProvider = { token: () => new Promise<string>(() => {}), origin: BASE };
      const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: server().fetch });
      const ctrl = new AbortController();
      const cancelled = t.post("memory.get", { org: "o" }, { signal: ctrl.signal, timeoutMs: 60_000 });
      await Promise.resolve();
      ctrl.abort();
      await expect(cancelled).rejects.toThrow("aborted");
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("times a caller out on a stalled token, and another caller still gets it", async () => {
    let release: (t: string) => void = () => {};
    const pending = new Promise<string>((r) => {
      release = r;
    });
    const provider: TokenProvider = { token: () => pending, origin: BASE };
    const srv = server();
    const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: srv.fetch });
    const hurried = t.post("memory.get", { org: "o" }, { timeoutMs: 10 });
    const patient = t.post("memory.get", { org: "o" });
    await expect(hurried).rejects.toBeInstanceOf(TimeoutError);
    release("shared");
    expect(await patient).toEqual({ ok: true });
    expect(v1(srv).map(auth)).toEqual(["Bearer shared"]);
  });

  it("bounds one attempt, token wait and response together, by timeoutMs", async () => {
    const provider: TokenProvider = {
      token: () => new Promise<string>((r) => setTimeout(() => r("t"), 100)),
      origin: BASE,
    };
    const slow: FetchLike = (_url, init) =>
      new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => resolve(new Response("{}", { status: 200 })), 150);
        init.signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
      });
    const t = new Transport({ baseUrl: BASE, tokenProvider: provider, fetch: slow, retry: { maxAttempts: 1 } });
    await expect(t.post("memory.get", { org: "o" }, { timeoutMs: 200 })).rejects.toBeInstanceOf(TimeoutError);
  });

  it("surfaces a second 401 instead of looping", async () => {
    const no = { status: 401, body: { detail: { code: "UNAUTHENTICATED" } } };
    const srv = server({ v1: [no, no, no] });
    const t = new Transport({ baseUrl: BASE, tokenProvider: bot(srv), fetch: srv.fetch });
    await expect(t.post("memory.get", {})).rejects.toBeInstanceOf(AuthError);
    expect(v1(srv)).toHaveLength(2);
  });

  it("refuses to send the token to another origin", async () => {
    const srv = server();
    const t = new Transport({ baseUrl: "http://other.test", tokenProvider: bot(srv), fetch: srv.fetch });
    await expect(t.post("memory.get", {})).rejects.toThrow(/refusing to send bot:b's token to other\.test/);
    expect(srv.calls).toHaveLength(0);
  });

  it("names the bot, reads claims from the token in hand, and refuses a second credential", async () => {
    const srv = server();
    const t = new Transport({ baseUrl: BASE, tokenProvider: bot(srv), fetch: srv.fetch });
    expect(t.hasCredential).toBe(true);
    expect(t.apiKeyHint).toBe("bot:b");
    expect(t.tokenClaims()).toBeUndefined();
    await t.post("memory.get", {});
    expect(t.tokenClaims()).toMatchObject({ org: "acme", namespaces: ["ci"], roles: ["writer"] });
    expect(JSON.stringify(t)).not.toContain(accessToken(1));
    expect(() => new Transport({ baseUrl: BASE, apiKey: "k", tokenProvider: bot(srv) })).toThrow(/not both/);
  });

  it("readEnv signs in as the bot when LOOM_CLIENT_ID is set and no key is given", () => {
    vi.stubEnv("LOOM_CLIENT_ID", "env-bot");
    vi.stubEnv("LOOM_WORKLOAD_TOKEN", "wl");
    expect(readEnv({ baseUrl: BASE }).tokenProvider).toBeInstanceOf(MachineLogin);
    expect(readEnv({ baseUrl: BASE, apiKey: "k" }).tokenProvider).toBeUndefined();
    vi.stubEnv("LOOM_API_KEY", "k");
    expect(readEnv({ baseUrl: BASE }).tokenProvider).toBeUndefined();
  });
});
