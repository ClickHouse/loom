/**
 * Tokens for a bot or a CI job registered with `loom bot create`: no `loom
 * login` and no long-lived secret. Mirrors the Python SDK's `loom.machine`.
 * With LOOM_CLIENT_ID set, the SDK proves either
 *
 * - a key: it signs a short client assertion (private_key_jwt). The key is a
 *   PEM file (LOOM_CLIENT_KEY_FILE), the PEM itself (LOOM_CLIENT_KEY), or an
 *   AWS Secrets Manager secret holding it (LOOM_CLIENT_KEY_SECRET, an id or
 *   ARN; needs `@aws-sdk/client-secrets-manager`), read again for each token
 *   so a rotated key is picked up; or
 * - its workload's own OIDC token: LOOM_WORKLOAD_TOKEN or
 *   LOOM_WORKLOAD_TOKEN_FILE, or asked of GitHub Actions
 *   (ACTIONS_ID_TOKEN_REQUEST_URL) for LOOM_WORKLOAD_AUDIENCE,
 *
 * and gets a token of an hour (less when the bot's maximum is shorter) from
 * /oauth/token, renewed before it runs out and kept in memory only. Node-only:
 * `node:crypto` and `node:fs` are loaded when a token is first fetched, so a
 * browser bundle never pulls them.
 */

import { AuthError, BackendUnavailable, busy, classed, LoomError, NETWORK_ERROR_CODE } from "./errors.js";
import { envVar } from "./runtime.js";

/** A credential fetched per request, e.g. a bot's renewing token. */
export interface TokenProvider {
  /** The token to send; a fresh one when `refused` is the token the server just answered 401 to. */
  token(refused?: string): string | Promise<string>;
  /** The token in hand, without fetching; undefined before the first. */
  current?(): string | undefined;
  /** How logs and `toJSON` name the credential, e.g. `bot:<client id>`. */
  readonly label?: string;
  /** When set, the transport sends the token to this origin only. */
  readonly origin?: string;
}

export const ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer";
export const TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange";
export const KEY_SOURCES = ["LOOM_CLIENT_KEY_FILE", "LOOM_CLIENT_KEY", "LOOM_CLIENT_KEY_SECRET"] as const;
export type KeySource = (typeof KEY_SOURCES)[number];

const RENEW_BEFORE_MS = 300_000;
const RETRY_AFTER_FAILURE_MS = 30_000;
const SECRETS_MANAGER = "@aws-sdk/client-secrets-manager";

type FetchFn = (input: string, init: RequestInit) => Promise<Response>;

export interface MachineLoginOptions {
  baseUrl: string;
  clientId: string;
  /** Where the key comes from; without one the bot proves a workload token. */
  key?: { source: KeySource; value: string };
  /** The audience a GitHub Actions OIDC token is asked for. */
  audience?: string;
  /** Injected fetch (tests). Default: globalThis.fetch. */
  fetch?: FetchFn;
  /** Injected clock in ms (tests). */
  now?: () => number;
  /** Injected module loader (tests): loads `node:*` and the optional AWS SDK. */
  importModule?: (name: string) => Promise<unknown>;
}

/** The bot login the environment describes, or undefined without LOOM_CLIENT_ID. */
export function machineLogin(
  baseUrl: string,
  opts: Omit<MachineLoginOptions, "baseUrl" | "clientId" | "key" | "audience"> = {},
): MachineLogin | undefined {
  const clientId = envVar("LOOM_CLIENT_ID")?.trim();
  if (!clientId) return undefined;
  const given = KEY_SOURCES.filter((name) => envVar(name)?.trim());
  if (given.length > 1) throw new AuthError(`bot ${clientId}: set one of ${given.join(", ")}, not several`);
  const source = given[0];
  return new MachineLogin({
    ...opts,
    baseUrl,
    clientId,
    key: source ? { source, value: envVar(source)?.trim() ?? "" } : undefined,
    audience: envVar("LOOM_WORKLOAD_AUDIENCE")?.trim() ?? "",
  });
}

/** Sends the bot's token (an hour, or the bot's shorter maximum), renewed before it expires and on a 401. */
export class MachineLogin implements TokenProvider {
  readonly baseUrl: string;
  readonly clientId: string;
  readonly label: string;
  readonly origin: string;
  readonly #key: { source: KeySource; value: string } | undefined;
  readonly #audience: string;
  readonly #fetch: FetchFn;
  readonly #now: () => number;
  readonly #import: (name: string) => Promise<unknown>;
  #token = "";
  #expiresAt = 0;
  #renewAt = 0;
  #tokenEndpoint = "";
  /** One token fetch at a time: concurrent calls wait on it. */
  #queue: Promise<unknown> = Promise.resolve();

  constructor(opts: MachineLoginOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.clientId = opts.clientId;
    this.label = `bot:${opts.clientId}`;
    this.origin = new URL(this.baseUrl).origin;
    this.#key = opts.key;
    this.#audience = opts.audience ?? "";
    const f = opts.fetch ?? (globalThis.fetch as FetchFn | undefined);
    if (!f) throw new LoomError("no fetch available: pass `fetch` (Node ≥ 18 has one)");
    this.#fetch = f;
    this.#now = opts.now ?? (() => Date.now());
    this.#import = opts.importModule ?? loadModule;
  }

  current(): string | undefined {
    return this.#token || undefined;
  }

  token(refused = ""): Promise<string> {
    const run = this.#queue.then(() => this.#tokenNow(refused));
    this.#queue = run.catch(() => undefined);
    return run;
  }

  /** What JSON.stringify and inspect show: never the token. */
  toJSON(): Record<string, unknown> {
    return { baseUrl: this.baseUrl, clientId: this.clientId };
  }

  async #tokenNow(refused: string): Promise<string> {
    if (this.#token && this.#token !== refused && this.#now() <= this.#renewAt) return this.#token;
    try {
      const [token, expiresAt] = await this.#fetchToken();
      this.#token = token;
      this.#expiresAt = expiresAt;
      // Five minutes before it runs out, or halfway for a token a short-lived CI proof capped.
      const life = expiresAt - this.#now();
      this.#renewAt = expiresAt - Math.min(RENEW_BEFORE_MS, life / 2);
    } catch (e) {
      // Loom busy or away, a key mid-rotation, a CI token run out: the token
      // in hand is good until it expires, and the next try waits a bit.
      if (!this.#token || this.#token === refused || this.#now() >= this.#expiresAt) throw e;
      this.#renewAt = Math.min(this.#expiresAt, this.#now() + RETRY_AFTER_FAILURE_MS);
    }
    return this.#token;
  }

  async #fetchToken(): Promise<[string, number]> {
    const form: Record<string, string> = this.#key
      ? {
          grant_type: "client_credentials",
          client_id: this.clientId,
          client_assertion_type: ASSERTION_TYPE,
          client_assertion: await this.#assertion(
            await this.#assertionAudience(),
            await this.#keyPem(this.#key),
            this.#key.source,
          ),
        }
      : {
          grant_type: TOKEN_EXCHANGE,
          client_id: this.clientId,
          subject_token: await this.#workloadToken(),
          subject_token_type: "urn:ietf:params:oauth:token-type:jwt",
        };
    const res = await this.#get(`${this.baseUrl}/oauth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams(form).toString(),
    });
    const body = await bodyObject(res, `bot ${this.clientId}`);
    if (res.status === 429 || res.status >= 500) throw answerError(`bot ${this.clientId}`, res.status);
    if (res.status !== 200 || typeof body.access_token !== "string") {
      const error = typeof body.error === "string" ? body.error : "";
      const description = typeof body.error_description === "string" ? body.error_description : "";
      throw new AuthError(`bot ${this.clientId}: ${error || res.status} ${description}`.trim(), {
        status: res.status,
        code: error,
        detail: { code: error, message: description, error, error_description: description },
      });
    }
    const expiresIn = Number(body.expires_in ?? 3600);
    return [body.access_token, this.#now() + (Number.isFinite(expiresIn) ? expiresIn : 3600) * 1000];
  }

  /** The token endpoint the server names itself by, which a bot reaching it by another URL must still sign for. */
  async #assertionAudience(): Promise<string> {
    if (!this.#tokenEndpoint) {
      const res = await this.#get(`${this.baseUrl}/.well-known/oauth-authorization-server`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw answerError(`bot ${this.clientId}: server metadata`, res.status);
      const meta = await bodyObject(res, `bot ${this.clientId}: server metadata`);
      if (typeof meta.token_endpoint !== "string") throw new AuthError("server metadata names no token_endpoint");
      this.#tokenEndpoint = meta.token_endpoint;
    }
    return this.#tokenEndpoint;
  }

  async #assertion(audience: string, pem: string, source: KeySource): Promise<string> {
    const crypto = (await this.#import("node:crypto")) as typeof import("node:crypto");
    let key: import("node:crypto").KeyObject;
    try {
      key = crypto.createPrivateKey(pem);
    } catch (e) {
      // An env file's literal "\n"s, say.
      const why = pem.includes("ENCRYPTED") ? "an encrypted key is not supported" : "not a PEM private key";
      throw new AuthError(`${source}: ${why}`, { cause: e });
    }
    const kind = key.asymmetricKeyType;
    const alg = kind === "ec" ? "ES256" : kind === "ed25519" ? "EdDSA" : "RS256";
    const now = Math.floor(this.#now() / 1000);
    const claims = {
      iss: this.clientId,
      sub: this.clientId,
      aud: audience,
      iat: now,
      exp: now + 120,
      jti: crypto.randomUUID().replace(/-/g, ""),
    };
    const input = `${b64url(JSON.stringify({ alg, typ: "JWT" }))}.${b64url(JSON.stringify(claims))}`;
    const data = Buffer.from(input);
    const sig =
      alg === "ES256"
        ? crypto.sign("sha256", data, { key, dsaEncoding: "ieee-p1363" })
        : crypto.sign(alg === "EdDSA" ? null : "sha256", data, key);
    return `${input}.${sig.toString("base64url")}`;
  }

  /** The bot's private key, from where LOOM_CLIENT_KEY_* says. */
  async #keyPem({ source, value }: { source: KeySource; value: string }): Promise<string> {
    if (source === "LOOM_CLIENT_KEY") return value;
    if (source === "LOOM_CLIENT_KEY_FILE") return this.#readFile(value);
    let sm: SecretsManagerModule;
    try {
      sm = (await this.#import(SECRETS_MANAGER)) as SecretsManagerModule;
    } catch (e) {
      throw new AuthError(`LOOM_CLIENT_KEY_SECRET needs \`npm install ${SECRETS_MANAGER}\``, { cause: e });
    }
    // An ARN names its region; a bare id takes the AWS SDK's default one.
    const region = value.startsWith("arn:") ? value.split(":")[3] : undefined;
    const client = new sm.SecretsManagerClient(region ? { region } : {});
    let out: { SecretString?: string };
    try {
      out = await client.send(new sm.GetSecretValueCommand({ SecretId: value }));
    } catch (e) {
      throw new BackendUnavailable(`reading ${value} from Secrets Manager: ${e instanceof Error ? e.message : e}`, {
        cause: e,
      });
    } finally {
      client.destroy?.();
    }
    if (typeof out.SecretString !== "string")
      throw new AuthError(`${value}: the key must be stored as text, not as binary`);
    return out.SecretString;
  }

  /** The workload's OIDC token: given, in a file (re-read, so a rotated one is picked up), or asked of GitHub Actions. */
  async #workloadToken(): Promise<string> {
    const given = envVar("LOOM_WORKLOAD_TOKEN")?.trim();
    if (given) return given;
    const path = envVar("LOOM_WORKLOAD_TOKEN_FILE")?.trim();
    if (path) return (await this.#readFile(path)).trim();
    const url = envVar("ACTIONS_ID_TOKEN_REQUEST_URL") ?? "";
    const bearer = envVar("ACTIONS_ID_TOKEN_REQUEST_TOKEN") ?? "";
    if (!(url && bearer && this.#audience))
      throw new AuthError(
        "LOOM_CLIENT_ID is set but there is no key (LOOM_CLIENT_KEY_FILE, LOOM_CLIENT_KEY, LOOM_CLIENT_KEY_SECRET) " +
          "or workload token (LOOM_WORKLOAD_TOKEN, LOOM_WORKLOAD_TOKEN_FILE, or a GitHub Actions job with " +
          "LOOM_WORKLOAD_AUDIENCE)",
      );
    // The runner's URL carries its own query (an API version): the audience joins it.
    const asked = new URL(url);
    asked.searchParams.set("audience", this.#audience);
    const res = await this.#get(asked.toString(), {
      headers: { Authorization: `Bearer ${bearer}`, Accept: "application/json" },
    });
    if (!res.ok) throw answerError("GitHub Actions OIDC token", res.status);
    const out = await bodyObject(res, "GitHub Actions OIDC token");
    if (typeof out.value !== "string") throw new AuthError("GitHub Actions answered no OIDC token");
    return out.value;
  }

  async #readFile(path: string): Promise<string> {
    const fs = (await this.#import("node:fs/promises")) as typeof import("node:fs/promises");
    return fs.readFile(path, "utf8");
  }

  /** One request of the login itself; a connection failure reads as the server being away. */
  async #get(url: string, init: RequestInit): Promise<Response> {
    let res: Response;
    try {
      // Never followed: a redirect would carry the proof in the body elsewhere.
      res = await this.#fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(30_000) });
    } catch (e) {
      // A client-credentials login spends nothing: asking again is safe.
      const err = new BackendUnavailable(`bot ${this.clientId}: cannot reach ${new URL(url).origin}`, {
        cause: e,
        code: NETWORK_ERROR_CODE,
      });
      throw classed(err, "dependency", true);
    }
    if (res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400))
      throw new AuthError(`bot ${this.clientId}: ${new URL(url).origin} answered a redirect; not followed`, {
        status: res.status,
      });
    return res;
  }
}

interface SecretsManagerModule {
  SecretsManagerClient: new (config: {
    region?: string;
  }) => { send(command: unknown): Promise<{ SecretString?: string }>; destroy?(): void };
  GetSecretValueCommand: new (input: { SecretId: string }) => unknown;
}

// A computed specifier, so a bundler neither pulls a Node built-in into a
// browser build nor fails on the optional AWS SDK when it is not installed.
function loadModule(name: string): Promise<unknown> {
  return import(/* webpackIgnore: true */ /* @vite-ignore */ name);
}

function b64url(s: string): string {
  return Buffer.from(s).toString("base64url");
}

/** Busy or away (429, 5xx) is retryable; any other answer is a refusal, as
 *  the Python SDK reads it. */
function answerError(what: string, status: number): LoomError {
  if (status === 429 || status >= 500) return busy(`${what}: ${status}; retry shortly`, { status });
  return new AuthError(`${what}: ${status}`, { status });
}

/** The answer's JSON object; {} for a body that is not one (a proxy's page,
 *  `null`), which the caller then refuses by the field it lacks. A body cut
 *  off in transit is BackendUnavailable, retried like a connection failure. */
async function bodyObject(res: Response, what: string): Promise<Record<string, unknown>> {
  let text: string;
  try {
    text = await res.text();
  } catch (e) {
    const err = new BackendUnavailable(`${what}: the answer was cut off`, { status: res.status, cause: e });
    throw classed(err, "dependency", true);
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
  } catch {
    /* not JSON */
  }
  return {};
}
