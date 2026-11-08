import { describe, expect, it } from "vitest";
import {
  BackendUnavailable,
  INVALID_RESPONSE_CODE,
  LoomError,
  NETWORK_ERROR_CODE,
  NotFound,
  RateLimited,
  TimeoutError,
  ValidationError,
} from "./errors.js";
import { Transport } from "./transport.js";

type Scripted = { status: number; body?: unknown; headers?: Record<string, string>; throw?: unknown };

function fakeFetch(script: Scripted[]) {
  const calls: Array<{ url: string; init: RequestInit; headers: Record<string, string> }> = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    calls.push({ url, init, headers: init.headers as Record<string, string> });
    const step = script.shift();
    if (!step) throw new Error("unscripted call");
    if (step.throw) throw step.throw;
    const text = step.body === undefined ? "" : typeof step.body === "string" ? step.body : JSON.stringify(step.body);
    return new Response(text, { status: step.status, headers: step.headers ?? {} });
  };
  return { fetch, calls };
}

const sleeps: number[] = [];
const sleep = async (ms: number) => {
  sleeps.push(ms);
};

function transport(script: Scripted[], extra: Partial<ConstructorParameters<typeof Transport>[0]> = {}) {
  const f = fakeFetch(script);
  const t = new Transport({
    baseUrl: "http://loom.test/",
    apiKey: "k",
    fetch: f.fetch,
    sleep,
    ...extra,
    retry: { backoffMs: 1, ...extra.retry },
  });
  return { t, calls: f.calls };
}

describe("Transport", () => {
  it("posts JSON to /v1/<op> with auth, request id and an idempotency key for idempotent writes", async () => {
    const { t, calls } = transport([{ status: 200, body: { ok: true } }]);
    const out = await t.post("memory.set", { org: "o", namespace: "n", memory_key: "k", value: "v" });
    expect(out).toEqual({ ok: true });
    expect(calls[0].url).toBe("http://loom.test/v1/memory.set");
    expect(calls[0].headers.Authorization).toBe("Bearer k");
    expect(calls[0].headers["X-Request-Id"]).toMatch(/.+/);
    expect(calls[0].headers["Idempotency-Key"]).toMatch(/.+/);
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ org: "o", namespace: "n", memory_key: "k", value: "v" });
  });

  it("sends no idempotency key for a read-only op unless the caller gives one", async () => {
    const { t, calls } = transport([
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    await t.post("memory.get", { org: "o", namespace: "n", memory_key: "k" });
    expect(calls[0].headers["Idempotency-Key"]).toBeUndefined();
    await t.post("memory.get", { org: "o" }, { idempotencyKey: "abc" });
    expect(calls[1].headers["Idempotency-Key"]).toBe("abc");
  });

  it("sends no idempotency key for a non-idempotent op unless the caller gives one", async () => {
    const { t, calls } = transport([
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    await t.post("memory.set_from_messages", { org: "o", namespace: "n", messages: [] });
    expect(calls[0].headers["Idempotency-Key"]).toBeUndefined();
    await t.post("memory.set_from_messages", { org: "o" }, { idempotencyKey: "abc" });
    expect(calls[1].headers["Idempotency-Key"]).toBe("abc");
  });

  it("retries a write whose first request still runs, with the same key, after Retry-After", async () => {
    sleeps.length = 0;
    const inProgress = { code: "IDEMPOTENT_IN_PROGRESS", message: "m", in_progress: true };
    const { t, calls } = transport([
      { status: 503, body: { detail: inProgress }, headers: { "Retry-After": "1" } },
      { status: 200, body: { ok: true } },
    ]);
    expect(await t.post("memory.set", { org: "o", namespace: "n", memory_key: "k", value: "v" })).toEqual({ ok: true });
    expect(calls[1].headers["Idempotency-Key"]).toBe(calls[0].headers["Idempotency-Key"]);
    expect(sleeps).toEqual([1000]);
  });

  it("adds the consumer label to the body unless the caller set one", async () => {
    const { t, calls } = transport(
      [
        { status: 200, body: {} },
        { status: 200, body: {} },
      ],
      { consumer: "support-bot" },
    );
    await t.post("memory.get", { org: "o" });
    expect(JSON.parse(calls[0].init.body as string).consumer).toBe("support-bot");
    await t.post("memory.get", { org: "o", consumer: "mine" });
    expect(JSON.parse(calls[1].init.body as string).consumer).toBe("mine");
  });

  it("treats an empty or sentinel consumer on the body as unnamed", async () => {
    const { t, calls } = transport(
      [
        { status: 200, body: {} },
        { status: 200, body: {} },
        { status: 200, body: {} },
      ],
      { consumer: "support-bot" },
    );
    await t.post("memory.get", { org: "o", consumer: "" });
    await t.post("memory.get", { org: "o", consumer: "-" });
    await t.post("memory.get", { org: "o", consumer: null });
    for (const c of calls) expect(JSON.parse(c.init.body as string).consumer).toBe("support-bot");
  });

  it("sends the body byte-for-byte when no consumer is configured", async () => {
    const { t, calls } = transport([{ status: 200, body: {} }]);
    await t.post("memory.get", { org: "o", namespace: "n", memory_key: "k" });
    expect(calls[0].init.body).toBe(JSON.stringify({ org: "o", namespace: "n", memory_key: "k" }));
  });

  it("retries a 5xx with backoff and returns the eventual 200", async () => {
    sleeps.length = 0;
    const { t, calls } = transport([
      { status: 503, body: { detail: { code: "X", message: "down" } } },
      { status: 200, body: { v: 1 } },
    ]);
    expect(await t.post("memory.get", {})).toEqual({ v: 1 });
    expect(calls.length).toBe(2);
    expect(sleeps.length).toBe(1);
  });

  it("never retries a 5xx that names a configuration fault", async () => {
    for (const code of ["CODED_WIDTH_MISMATCH", "GATEWAY_REQUIRED", "MODEL_CALL_UNATTRIBUTED"]) {
      sleeps.length = 0;
      const { t, calls } = transport([
        { status: 503, body: { detail: { code, message: "fix the deployment" } } },
        { status: 200, body: {} },
      ]);
      await expect(t.post("memory.get", {})).rejects.toMatchObject({ code });
      expect(calls.length).toBe(1);
      expect(sleeps.length).toBe(0);
    }
  });

  it("names the unreachable base URL and sets code NETWORK when fetch itself fails", async () => {
    const cause = new TypeError("fetch failed", { cause: { code: "ECONNREFUSED" } });
    const { t, calls } = transport([{ status: 0, throw: cause }], { retry: { retryConnectionErrors: false } });
    const err = await t.post("memory.get", {}).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(BackendUnavailable);
    const e = err as BackendUnavailable;
    expect(e.message).toBe("memory.get: cannot reach http://loom.test: fetch failed");
    expect(e.code).toBe(NETWORK_ERROR_CODE);
    expect(e.status).toBe(0);
    expect(e.cause).toBe(cause);
    expect(e.requestId).toBe(calls[0].headers["X-Request-Id"]);
    // The server never saw it: resending is safe.
    expect([e.category, e.retryable]).toEqual(["dependency", true]);
  });

  it("resends a dropped socket only on an op the server dedups", async () => {
    const dropped = { status: 0, throw: new TypeError("fetch failed", { cause: { code: "UND_ERR_SOCKET" } }) };
    for (const [op, retryable] of [
      ["memory.set_from_messages", false],
      ["memory.ask", true],
    ] as const) {
      const { t, calls } = transport([dropped, { status: 200, body: {} }]);
      const err = (await t.post(op, {}).catch((e: unknown) => e)) as LoomError;
      expect([err.code, err.retryable, calls.length]).toEqual([NETWORK_ERROR_CODE, retryable, 1]);
    }
    const { t, calls } = transport([dropped, { status: 200, body: {} }]);
    await expect(t.post("memory.set", {})).resolves.toEqual({});
    expect(calls.length).toBe(2);
  });

  it.each([
    [{ retryTimeouts: "never" }, "memory.set", 1],
    [{ retryTimeouts: "always" }, "memory.set_from_messages", 2],
    [{ retryConnectionErrors: false }, "memory.set", 2],
  ] as const)("resends a dropped socket under %o as the timeout setting says", async (retry, op, sent) => {
    const dropped = { status: 0, throw: new TypeError("fetch failed", { cause: { code: "UND_ERR_SOCKET" } }) };
    const { t, calls } = transport([dropped, { status: 200, body: {} }], { retry });
    await t.post(op, {}).catch(() => undefined);
    expect(calls.length).toBe(sent);
  });

  it.each(["localhost:7777", "not a url", "ftp://loom.test", "http://u:p@loom.test", "http://"])(
    "refuses baseUrl %s when it is built",
    (baseUrl) => {
      let err: unknown;
      try {
        new Transport({ baseUrl });
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(LoomError);
      expect([(err as LoomError).category, (err as LoomError).retryable]).toEqual(["input", false]);
      expect((err as LoomError).message).not.toContain("u:p");
    },
  );

  it("surfaces BackendUnavailable after the last 5xx and never retries a 4xx", async () => {
    const { t, calls } = transport([
      { status: 500 },
      { status: 502 },
      { status: 500, body: { detail: { code: "E", message: "boom" } } },
    ]);
    await expect(t.post("memory.get", {})).rejects.toBeInstanceOf(BackendUnavailable);
    expect(calls.length).toBe(3);
    const { t: t2, calls: c2 } = transport([
      { status: 404, body: { detail: { code: "NOT_FOUND", message: "no such key" } } },
    ]);
    await expect(t2.post("memory.get", {})).rejects.toBeInstanceOf(NotFound);
    expect(c2.length).toBe(1);
  });

  it("keeps retryable on a 5xx the handler never ran, or one the server calls retryable", async () => {
    const unloaded = { status: 503, body: { detail: { code: "AUTH_UNAVAILABLE", message: "m" } } };
    const { t } = transport([unloaded, unloaded, unloaded]);
    const err = (await t.post("memory.set_from_messages", {}).catch((e: unknown) => e)) as LoomError;
    expect([err.category, err.retryable]).toEqual(["dependency", true]);
    const detail = { code: "MODEL_CHANGE_IN_PROGRESS", message: "m", category: "capacity", retryable: true };
    const named = { status: 503, body: { detail } };
    const { t: t2 } = transport([named, named, named]);
    const err2 = (await t2.post("memory.set_from_messages", {}).catch((e: unknown) => e)) as LoomError;
    expect([err2.category, err2.retryable]).toEqual(["capacity", true]);
  });

  it("classes a fieldless 5xx as not retryable on an op that is not idempotent, and retryable on a read", async () => {
    // memory.set_from_messages is not idempotent: the 503 may follow a write it made.
    const { t } = transport([{ status: 503, body: { detail: { code: "BACKEND_UNAVAILABLE", message: "m" } } }]);
    const err = (await t.post("memory.set_from_messages", {}).catch((e: unknown) => e)) as LoomError;
    expect([err.category, err.retryable]).toEqual(["dependency", false]);
    const { t: t2 } = transport([
      { status: 503 },
      { status: 503 },
      { status: 503, body: { detail: { code: "BACKEND_UNAVAILABLE", message: "m" } } },
    ]);
    const read = (await t2.post("memory.get", {}).catch((e: unknown) => e)) as LoomError;
    expect([read.category, read.retryable]).toEqual(["dependency", true]);
  });

  it("does not retry an answer the server classes configuration", async () => {
    const detail = { code: "SCHEMA_PENDING", message: "m", category: "configuration", retryable: false };
    const { t, calls } = transport([
      { status: 503, body: { detail } },
      { status: 200, body: {} },
    ]);
    const err = (await t.post("memory.get", {}).catch((e: unknown) => e)) as LoomError;
    expect([err.retryable, calls.length]).toEqual([false, 1]);
  });

  it("classes a fieldless OVERLOADED by its status, retryable only on an idempotent op", async () => {
    // A handler's OVERLOADED may follow a statement that wrote, as the server's rule says.
    const overloaded = { status: 503, body: { detail: { code: "OVERLOADED", message: "m" } } };
    const { t } = transport([overloaded]);
    const err = (await t.post("memory.set_from_messages", {}).catch((e: unknown) => e)) as LoomError;
    expect([err.category, err.retryable]).toEqual(["dependency", false]);
    const { t: t2 } = transport([overloaded, overloaded, overloaded]);
    const read = (await t2.post("memory.get", {}).catch((e: unknown) => e)) as LoomError;
    expect([read.category, read.retryable]).toEqual(["dependency", true]);
  });

  it("maps 422 to ValidationError carrying code, message, status and request id", async () => {
    const { t } = transport([
      {
        status: 422,
        body: { detail: { code: "VALIDATION", message: "query required" } },
        headers: { "X-Request-Id": "rid-1" },
      },
    ]);
    const err = (await t.post("memory.search", {}).catch((e) => e)) as ValidationError;
    expect(err).toBeInstanceOf(ValidationError);
    expect(err.code).toBe("VALIDATION");
    expect(err.message).toBe("query required");
    expect(err.status).toBe(422);
    expect(err.requestId).toBe("rid-1");
    expect(err.op).toBe("memory.search");
  });

  it("waits Retry-After on a 429 and retries, then surfaces RateLimited past the budget", async () => {
    let clock = 0;
    const now = () => clock;
    const s: number[] = [];
    const slp = async (ms: number) => {
      s.push(ms);
      clock += ms;
    };
    const f = fakeFetch([
      {
        status: 429,
        headers: { "Retry-After": "2" },
        body: { detail: { code: "RATE_LIMITED", message: "slow down" } },
      },
      { status: 200, body: { ok: 1 } },
    ]);
    const t = new Transport({ baseUrl: "http://x", fetch: f.fetch, sleep: slp, now, ratelimitMaxWaitS: 5 });
    expect(await t.post("memory.get", {})).toEqual({ ok: 1 });
    expect(s).toEqual([2000]);
    const f2 = fakeFetch([{ status: 429, headers: { "Retry-After": "30" }, body: {} }]);
    const t2 = new Transport({ baseUrl: "http://x", fetch: f2.fetch, sleep: slp, now, ratelimitMaxWaitS: 5 });
    const err = (await t2.post("memory.get", {}).catch((e) => e)) as RateLimited;
    expect(err).toBeInstanceOf(RateLimited);
    expect(err.retryAfterS).toBe(30);
  });

  it("retries a connection failure but not a timeout on a non-idempotent op", async () => {
    const { t, calls } = transport([
      { status: 0, throw: new TypeError("fetch failed", { cause: { code: "ECONNREFUSED" } }) },
      { status: 200, body: {} },
    ]);
    await t.post("memory.get", {});
    expect(calls.length).toBe(2);
    // A timeout on a NON-idempotent op is never retried: the server may still be executing it.
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    const { t: t2, calls: c2 } = transport([{ status: 0, throw: abort }]);
    await expect(t2.post("memory.set_from_messages", {}, { timeoutMs: 5 })).rejects.toBeInstanceOf(TimeoutError);
    expect(c2.length).toBe(1);
  });

  it("classes a token that never arrives as dependency and retryable: the call was never sent", async () => {
    const f = fakeFetch([{ status: 200, body: {} }]);
    const t = new Transport({
      baseUrl: "http://loom.test/",
      tokenProvider: { token: () => new Promise<string>(() => {}) },
      fetch: f.fetch,
      sleep,
    });
    const err = (await t.post("memory.get", {}, { timeoutMs: 5 }).catch((e: unknown) => e)) as TimeoutError;
    expect(err).toBeInstanceOf(TimeoutError);
    expect([err.category, err.retryable]).toEqual(["dependency", true]);
    expect(f.calls.length).toBe(0);
  });

  it("calls a timed-out call retryable only on an idempotent op, a read included", async () => {
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    for (const [op, retryable] of [
      ["memory.set", true],
      ["memory.search", true],
      ["memory.set_from_messages", false],
    ] as const) {
      const { t } = transport([{ status: 0, throw: abort }], { retry: { retryTimeouts: "never" } });
      const err = (await t.post(op, {}, { timeoutMs: 5 }).catch((e: unknown) => e)) as TimeoutError;
      expect([err.category, err.retryable]).toEqual(["dependency", retryable]);
    }
  });

  it("paces the next call when the last response said the budget is at the floor", async () => {
    let clock = 0;
    const s: number[] = [];
    const f = fakeFetch([
      {
        status: 200,
        body: {},
        headers: { "X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3" },
      },
      { status: 200, body: {} },
    ]);
    const t = new Transport({
      baseUrl: "http://x",
      fetch: f.fetch,
      sleep: async (ms) => {
        s.push(ms);
        clock += ms;
      },
      now: () => clock,
    });
    await t.post("memory.search", {});
    expect(t.rateState("memory.search")).toEqual({ limit: 10, remaining: 0, resetAtMs: 3000 });
    await t.post("memory.search", {});
    expect(s).toEqual([3000]);
  });
});

describe("Transport cancellation and the whole-call timeout", () => {
  /** A 200 whose body arrives after `delayMs`; `wired` errors it the way fetch does when its signal fires. */
  function slowBody(delayMs: number, calls: { n: number }, wired = true) {
    return async (_url: string, init: RequestInit): Promise<Response> => {
      calls.n++;
      let t: ReturnType<typeof setTimeout> | undefined;
      const stream = new ReadableStream<Uint8Array>({
        cancel() {
          clearTimeout(t);
        },
        start(c) {
          t = setTimeout(() => {
            c.enqueue(new TextEncoder().encode("{}"));
            c.close();
          }, delayMs);
          if (wired)
            init.signal?.addEventListener("abort", () => {
              clearTimeout(t);
              c.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
            });
        },
      });
      return new Response(stream, { status: 200 });
    };
  }

  it("sends nothing for a signal that is already aborted", async () => {
    const calls = { n: 0 };
    const t = new Transport({ baseUrl: "http://x", fetch: slowBody(0, calls), logLevel: "off" });
    const c = new AbortController();
    c.abort();
    await expect(t.post("memory.get", {}, { signal: c.signal })).rejects.toThrow(/memory\.get: aborted by caller/);
    expect(calls.n).toBe(0);
  });

  it("stops a retry backoff and a 429 wait when the caller aborts", async () => {
    for (const status of [503, 429]) {
      let n = 0;
      const fetch = async () => {
        n++;
        return new Response("{}", { status, headers: { "Retry-After": "10" } });
      };
      const t = new Transport({ baseUrl: "http://x", fetch, logLevel: "off", retry: { backoffMs: 10_000 } });
      const c = new AbortController();
      setTimeout(() => c.abort(), 20);
      const t0 = Date.now();
      const err = await t.post("memory.get", {}, { signal: c.signal }).catch((e: unknown) => e);
      expect(err, String(status)).toBeInstanceOf(LoomError);
      expect((err as LoomError).message).toMatch(/aborted by caller/);
      expect([(err as LoomError).category, (err as LoomError).retryable]).toEqual(["input", false]);
      expect(Date.now() - t0).toBeLessThan(2_000);
      expect(n).toBe(1);
    }
  });

  it("applies the timeout and the caller's signal to the body read, even when the fetch ignores the signal", async () => {
    for (const wired of [true, false]) {
      const t = new Transport({
        baseUrl: "http://x",
        fetch: slowBody(5_000, { n: 0 }, wired),
        logLevel: "off",
        retry: { maxAttempts: 1 },
      });
      await expect(t.post("memory.set_from_messages", {}, { timeoutMs: 20 })).rejects.toBeInstanceOf(TimeoutError);
      const c = new AbortController();
      setTimeout(() => c.abort(), 20);
      await expect(t.post("memory.get", {}, { signal: c.signal })).rejects.toThrow(/aborted by caller/);
    }
  });

  it("names a body cut off mid-stream and resends it only on an op the server dedups", async () => {
    let n = 0;
    const fetch = async () => {
      n++;
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          c.error(new TypeError("terminated"));
        },
      });
      return new Response(stream, { status: 200 });
    };
    const t = new Transport({ baseUrl: "http://x", fetch, logLevel: "off", sleep: async () => {} });
    for (const [op, retryable, sent] of [
      ["memory.set", true, 3],
      ["memory.ask", true, 1],
      ["memory.set_from_messages", false, 1],
    ] as const) {
      n = 0;
      const err = (await t.post(op, {}).catch((e: unknown) => e)) as BackendUnavailable;
      expect(err).toBeInstanceOf(BackendUnavailable);
      expect([err.code, err.category, err.retryable]).toEqual([NETWORK_ERROR_CODE, "dependency", retryable]);
      expect(err.message).toMatch(/cut off: terminated/);
      expect(n).toBe(sent);
    }
  });
});

describe("Transport response decoding", () => {
  it("turns a 2xx body that is not JSON into a LoomError with code INVALID_RESPONSE, not retried", async () => {
    let n = 0;
    const fetch = async () => {
      n++;
      return new Response("<html>502 from the proxy</html>", { status: 200, headers: { "X-Request-Id": "rid-1" } });
    };
    const t = new Transport({ baseUrl: "http://x", fetch, logLevel: "off", sleep: async () => {} });
    const err = (await t.post("memory.get", {}).catch((e: unknown) => e)) as LoomError;
    expect(err).toBeInstanceOf(LoomError);
    expect(err).not.toBeInstanceOf(SyntaxError);
    expect(err.code).toBe(INVALID_RESPONSE_CODE);
    expect(err.status).toBe(200);
    expect(err.requestId).toBe("rid-1");
    expect(err.detail.message).toBe("<html>502 from the proxy</html>");
    expect(n).toBe(1);
  });

  it("reads an empty 2xx body as {}", async () => {
    const t = new Transport({
      baseUrl: "http://x",
      fetch: async () => new Response("", { status: 200 }),
      logLevel: "off",
    });
    expect(await t.post("memory.get", {})).toEqual({});
  });
});
