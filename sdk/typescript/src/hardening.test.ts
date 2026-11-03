/**
 * The credential is invisible to stringify, a blank key is no
 * key, every request identifies the SDK, Retry-After parses as seconds or an
 * HTTP-date, and postWithMeta / callWithResponse return the response metadata.
 */

import { inspect } from "node:util";
import { describe, expect, it } from "vitest";
import { LoomClient, readEnv } from "./client.js";
import { RateLimited } from "./errors.js";
import { DEFAULT_RETRY_AFTER_S, parseRetryAfter, retryAfterFrom } from "./retry-after.js";
import { runtimeIdent } from "./runtime.js";
import { Transport } from "./transport.js";
import { SDK_IDENT, SDK_VERSION } from "./version.js";

type Scripted = { status: number; body?: unknown; headers?: Record<string, string> };

function fakeFetch(script: Scripted[]) {
  const calls: Array<{ url: string; headers: Record<string, string>; body: string }> = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    calls.push({ url, headers: { ...(init.headers as Record<string, string>) }, body: String(init.body) });
    const step = script.shift();
    if (!step) throw new Error("unscripted call");
    const text = step.body === undefined ? "" : JSON.stringify(step.body);
    return new Response(text, { status: step.status, headers: step.headers ?? {} });
  };
  return { fetch, calls };
}

const KEY = "sk-loom-very-secret-1234";

describe("credential hygiene", () => {
  it("never shows the key through JSON.stringify, inspect or own properties", () => {
    const f = fakeFetch([]);
    const t = new Transport({ baseUrl: "http://x", apiKey: KEY, fetch: f.fetch });
    const client = new LoomClient(t);
    for (const s of [
      JSON.stringify(t),
      JSON.stringify(client),
      inspect(t, { depth: 5 }),
      inspect(client, { depth: 5 }),
    ]) {
      expect(s).not.toContain(KEY);
    }
    expect(Object.values(Object.getOwnPropertyDescriptors(t)).some((d) => String(d.value ?? "").includes(KEY))).toBe(
      false,
    );
    expect(t.hasCredential).toBe(true);
    expect(t.apiKeyHint).toBe("…1234");
    expect(JSON.stringify(t)).toContain("…1234");
  });

  it("still sends the bearer on the wire, and a blank key sends none", async () => {
    const f = fakeFetch([{ status: 200, body: {} }]);
    const t = new Transport({ baseUrl: "http://x", apiKey: KEY, fetch: f.fetch });
    await t.post("memory.get", {});
    expect(f.calls[0].headers.Authorization).toBe(`Bearer ${KEY}`);

    const f2 = fakeFetch([{ status: 200, body: {} }]);
    const t2 = new Transport({ baseUrl: "http://x", apiKey: "   ", fetch: f2.fetch });
    expect(t2.hasCredential).toBe(false);
    expect(t2.apiKeyHint).toBe("none");
    await t2.post("memory.get", {});
    expect("Authorization" in f2.calls[0].headers).toBe(false);
  });

  it("sends apiKey as the only credential, whatever the case of a caller's Authorization header", async () => {
    for (const name of ["Authorization", "authorization", "AUTHORIZATION"]) {
      const f = fakeFetch([
        { status: 200, body: {} },
        { status: 200, body: {} },
      ]);
      await new Transport({
        baseUrl: "http://x",
        apiKey: KEY,
        headers: { [name]: "Bearer stale" },
        fetch: f.fetch,
      }).post("memory.get", {});
      await new Transport({ baseUrl: "http://x", headers: { [name]: "Bearer stale" }, fetch: f.fetch }).post(
        "memory.get",
        {},
      );
      expect(new Headers(f.calls[0].headers).get("authorization"), name).toBe(`Bearer ${KEY}`);
      expect(new Headers(f.calls[1].headers).get("authorization"), name).toBeNull();
    }
  });

  it("readEnv treats a blank LOOM_API_KEY as unset", () => {
    const env = (globalThis as { process: { env: Record<string, string | undefined> } }).process.env;
    const saved = { k: env.LOOM_API_KEY, b: env.LOOM_BASE_URL };
    try {
      env.LOOM_API_KEY = " tok-from-dashboard ";
      env.LOOM_BASE_URL = " http://env ";
      expect(readEnv({ baseUrl: "" })).toEqual({ baseUrl: "http://env", apiKey: "tok-from-dashboard" });
      env.LOOM_API_KEY = "   ";
      expect(readEnv({ baseUrl: "http://x" }).apiKey).toBeUndefined();
      expect(readEnv({ baseUrl: "http://x", apiKey: " explicit " }).apiKey).toBe("explicit");
    } finally {
      env.LOOM_API_KEY = saved.k;
      env.LOOM_BASE_URL = saved.b;
    }
  });
});

describe("Retry-After", () => {
  it.each([
    ["2", 2],
    [" 0.5 ", 0.5],
    ["0", 0],
    ["-3", 0],
    ["", undefined],
    [null, undefined],
    ["soon", undefined],
  ])("parses %j as %j", (header, expected) => {
    expect(parseRetryAfter(header as string | null)).toBe(expected);
  });

  it("parses an HTTP-date relative to now and clamps the past to 0", () => {
    const now = Date.UTC(2026, 9, 21, 7, 28, 0);
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:29:30 GMT", now)).toBe(90);
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:00:00 GMT", now)).toBe(0);
  });

  it("reads a zoneless HTTP-date (asctime, -0000) as UTC whatever the local zone", () => {
    const saved = process.env.TZ;
    process.env.TZ = "America/Los_Angeles";
    try {
      const now = Date.UTC(2026, 9, 21, 7, 28, 0);
      expect(parseRetryAfter("Wed Oct 21 07:29:30 2026", now)).toBe(90);
      expect(parseRetryAfter("Wed, 21 Oct 2026 07:29:30 -0000", now)).toBe(90);
    } finally {
      if (saved === undefined) delete process.env.TZ;
      else process.env.TZ = saved;
    }
  });

  it("caps an oversized hint and defaults an unparseable one", () => {
    expect(retryAfterFrom(new Headers({ "Retry-After": "7" }))).toBe(7);
    expect(retryAfterFrom(new Headers({ "Retry-After": "3600" }))).toBe(DEFAULT_RETRY_AFTER_S);
    expect(retryAfterFrom(new Headers())).toBe(DEFAULT_RETRY_AFTER_S);
    expect(retryAfterFrom(new Headers({ "Retry-After": "garbage" }))).toBe(DEFAULT_RETRY_AFTER_S);
  });

  it("honours an HTTP-date on a 429 instead of silently sleeping one second", async () => {
    let clock = Date.UTC(2026, 9, 21, 7, 28, 0);
    const sleeps: number[] = [];
    const f = fakeFetch([
      {
        status: 429,
        headers: { "Retry-After": "Wed, 21 Oct 2026 07:28:03 GMT" },
        body: { detail: { code: "RATE_LIMITED", message: "slow" } },
      },
      { status: 200, body: { ok: 1 } },
    ]);
    const t = new Transport({
      baseUrl: "http://x",
      fetch: f.fetch,
      now: () => clock,
      sleep: async (ms) => {
        sleeps.push(ms);
        clock += ms;
      },
      ratelimitMaxWaitS: 10,
    });
    expect(await t.post("memory.set", {})).toEqual({ ok: 1 });
    expect(sleeps).toEqual([3000]);

    const f2 = fakeFetch([{ status: 429, headers: { "Retry-After": "Wed, 21 Oct 2026 07:29:00 GMT" }, body: {} }]);
    const t2 = new Transport({
      baseUrl: "http://x",
      fetch: f2.fetch,
      now: () => clock,
      sleep: async () => {},
      ratelimitMaxWaitS: 5,
    });
    const err = (await t2.post("memory.set", {}).catch((e) => e)) as RateLimited;
    expect(err).toBeInstanceOf(RateLimited);
    expect(err.retryAfterS).toBe(57);
  });
});

describe("identifying headers and the response envelope", () => {
  it("names the SDK, runtime and attempt on every request", async () => {
    const f = fakeFetch([
      { status: 503, body: {} },
      { status: 200, body: {} },
    ]);
    const t = new Transport({ baseUrl: "http://x", fetch: f.fetch, sleep: async () => {}, retry: { backoffMs: 1 } });
    await t.post("memory.get", {});
    expect(f.calls[0].headers["User-Agent"]).toBe(SDK_IDENT);
    expect(f.calls[0].headers["X-Loom-SDK"]).toBe(`loom-ts/${SDK_VERSION}`);
    expect(f.calls[0].headers["X-Loom-Runtime"]).toBe(runtimeIdent());
    expect(runtimeIdent()).toMatch(/^node\/\d+\.\d+\.\d+ /);
    expect(f.calls.map((c) => c.headers["X-Loom-Retry-Count"])).toEqual(["0", "1"]);
    expect(f.calls[0].headers["X-Request-Id"]).toBe(f.calls[1].headers["X-Request-Id"]);
  });

  it("counts attempts across a 429 pacing wait", async () => {
    const f = fakeFetch([
      { status: 503, body: {} },
      { status: 429, headers: { "Retry-After": "1" }, body: {} },
      { status: 503, body: {} },
      { status: 200, body: {} },
    ]);
    const t = new Transport({ baseUrl: "http://x", fetch: f.fetch, sleep: async () => {}, retry: { backoffMs: 1 } });
    const r = await t.postWithMeta("memory.get", {});
    expect(f.calls.map((c) => c.headers["X-Loom-Retry-Count"])).toEqual(["0", "1", "2", "3"]);
    expect(r.attempts).toBe(4);
  });

  it("sends a caller's request id only in the form the server keeps", async () => {
    const f = fakeFetch([
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    const t = new Transport({ baseUrl: "http://x", fetch: f.fetch });
    await t.postWithMeta("memory.get", {}, { requestId: "dbg-0001" });
    await t.postWithMeta("memory.get", {}, { requestId: "my id" });
    expect(f.calls[0].headers["X-Request-Id"]).toBe("dbg-0001");
    expect(f.calls[1].headers["X-Request-Id"]).not.toBe("my id");
    expect(f.calls[1].headers["X-Request-Id"]).toMatch(/^[A-Za-z0-9._-]{1,64}$/);
  });

  it("postWithMeta and callWithResponse return request id, schema version, timing and rate limit", async () => {
    const f = fakeFetch([
      {
        status: 200,
        body: { hits: [] },
        headers: {
          "X-Request-Id": "srv-1",
          "X-Schema-Version": "2026-09-16",
          "Server-Timing": 'app;dur=12.5, ch;dur=3.1;desc="2 statements, 4 kB"',
          "X-RateLimit-Limit": "100",
          "X-RateLimit-Remaining": "99",
          "X-RateLimit-Reset": "5",
        },
      },
      { status: 200, body: { found: false } },
    ]);
    const t = new Transport({ baseUrl: "http://x", fetch: f.fetch, now: () => 1000 });
    const r = await t.postWithMeta<{ hits: unknown[] }>("memory.search", { org: "o" }, { requestId: "srv-1" });
    expect(r).toEqual({
      data: { hits: [] },
      status: 200,
      requestId: "srv-1",
      schemaVersion: "2026-09-16",
      serverTimingMs: 12.5,
      rateLimit: { limit: 100, remaining: 99, resetAtMs: 6000 },
      attempts: 1,
    });
    const client = new LoomClient(t);
    const r2 = await client.callWithResponse("memory.get", { org: "o", namespace: "n", agent: "a", memory_key: "k" });
    expect(r2.data.found).toBe(false);
    expect(r2.serverTimingMs).toBeUndefined();
    expect(r2.rateLimit).toBeUndefined();
    expect(r2.requestId).toMatch(/.+/);
  });
});
