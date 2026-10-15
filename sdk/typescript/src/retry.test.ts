/** The retry policy as an object, per-call override, timeouts only under idempotency. */
import { describe, expect, it } from "vitest";
import { BackendUnavailable, LoomError, TimeoutError } from "./errors.js";
import { backoffDelayMs, DEFAULT_RETRY_POLICY, resolveRetryPolicy } from "./retry.js";
import { Transport } from "./transport.js";

type Scripted = { status: number; body?: unknown; headers?: Record<string, string>; throw?: unknown };

function fakeFetch(script: Scripted[]) {
  const calls: Array<{ headers: Record<string, string> }> = [];
  const fetch = async (_url: string, init: RequestInit): Promise<Response> => {
    calls.push({ headers: { ...(init.headers as Record<string, string>) } });
    const step = script.shift();
    if (!step) throw new Error("unscripted call");
    if (step.throw) throw step.throw;
    return new Response(step.body === undefined ? "" : JSON.stringify(step.body), {
      status: step.status,
      headers: step.headers ?? {},
    });
  };
  return { fetch, calls };
}

function harness(script: Scripted[], extra: Partial<ConstructorParameters<typeof Transport>[0]> = {}) {
  const sleeps: number[] = [];
  let clock = 0;
  const f = fakeFetch(script);
  const t = new Transport({
    baseUrl: "http://x",
    fetch: f.fetch,
    sleep: async (ms) => {
      sleeps.push(ms);
      clock += ms;
    },
    now: () => clock,
    logLevel: "off",
    ...extra,
  });
  return { t, calls: f.calls, sleeps };
}

const ABORT = Object.assign(new Error("aborted"), { name: "AbortError" });

describe("resolveRetryPolicy", () => {
  it("defaults: 3 attempts, 408 + 5xx, timeouts only for idempotent ops, 60 s Retry-After cap", () => {
    expect(DEFAULT_RETRY_POLICY.maxAttempts).toBe(3);
    expect(DEFAULT_RETRY_POLICY.statuses.has(408)).toBe(true);
    expect(DEFAULT_RETRY_POLICY.statuses.has(503)).toBe(true);
    expect(DEFAULT_RETRY_POLICY.statuses.has(429)).toBe(false);
    expect(DEFAULT_RETRY_POLICY.retryTimeouts).toBe("idempotent");
    expect(DEFAULT_RETRY_POLICY.maxRetryAfterS).toBe(60);
  });

  it("merges an override and names an invalid field", () => {
    const p = resolveRetryPolicy(DEFAULT_RETRY_POLICY, { maxAttempts: 5, statuses: [502, 503], backoffJitter: 0 });
    expect(p.maxAttempts).toBe(5);
    expect(Array.from(p.statuses)).toEqual([502, 503]);
    expect(p.backoffMs).toBe(250);
    expect(() => resolveRetryPolicy(DEFAULT_RETRY_POLICY, { maxAttempts: 0 })).toThrow(
      /retry\.maxAttempts must be an integer ≥ 1, got 0/,
    );
    expect(() => resolveRetryPolicy(DEFAULT_RETRY_POLICY, { backoffMs: -1 })).toThrow(/retry\.backoffMs/);
    expect(() => resolveRetryPolicy(DEFAULT_RETRY_POLICY, { backoffJitter: 2 })).toThrow(
      /backoffJitter must be within 0\.\.1/,
    );
    expect(() => resolveRetryPolicy(DEFAULT_RETRY_POLICY, { retryTimeouts: "sometimes" as never })).toThrow(
      /retryTimeouts/,
    );
    expect(() => resolveRetryPolicy(DEFAULT_RETRY_POLICY, { statuses: [42] })).toThrow(/non-HTTP status 42/);
    expect(
      () => new Transport({ baseUrl: "http://x", fetch: async () => new Response(), retry: { maxAttempts: 1.5 } }),
    ).toThrow(LoomError);
  });

  it("backoff doubles, is capped and jittered inside the band", () => {
    const p = resolveRetryPolicy(DEFAULT_RETRY_POLICY, { backoffMs: 100, backoffMaxMs: 350, backoffJitter: 0.5 });
    expect(backoffDelayMs(p, 1, () => 0.5)).toBe(100);
    expect(backoffDelayMs(p, 2, () => 0.5)).toBe(200);
    expect(backoffDelayMs(p, 3, () => 0.5)).toBe(350);
    expect(backoffDelayMs(p, 1, () => 0)).toBe(75);
    expect(backoffDelayMs(p, 1, () => 1)).toBe(125);
  });
});

describe("Transport with a policy", () => {
  it("honours Retry-After on a retried 503 and falls back to backoff beyond the cap", async () => {
    const { t, sleeps } = harness(
      [
        { status: 503, headers: { "Retry-After": "2" }, body: { detail: { code: "OVERLOADED", message: "busy" } } },
        { status: 503, headers: { "Retry-After": "3600" }, body: { detail: { code: "OVERLOADED", message: "busy" } } },
        { status: 200, body: { ok: 1 } },
      ],
      { retry: { backoffMs: 100, backoffJitter: 0 } },
    );
    expect(await t.post("memory.get", {})).toEqual({ ok: 1 });
    expect(sleeps).toEqual([2000, 200]);
  });

  it("waits an OVERLOADED 503's hint from the header or, without one, from the body", async () => {
    for (const headers of [{ "Retry-After": "3" }, {}] as Record<string, string>[]) {
      const detail = { code: "OVERLOADED", message: "busy", retry_after_s: 3 };
      const { t, sleeps } = harness(
        [
          { status: 503, headers, body: { detail } },
          { status: 200, body: {} },
        ],
        {
          retry: { backoffMs: 100, backoffJitter: 0 },
        },
      );
      await t.post("memory.get", {});
      expect(sleeps).toEqual([3000]);
    }
  });

  it("a per-call override disables retries for one call and the client policy stays", async () => {
    const { t, calls } = harness([{ status: 502 }, { status: 502 }, { status: 200, body: {} }]);
    await expect(t.post("memory.get", {}, { retry: { maxAttempts: 1 } })).rejects.toBeInstanceOf(BackendUnavailable);
    expect(calls.length).toBe(1);
    await t.post("memory.get", {});
    expect(calls.length).toBe(3);
  });

  it("retries a 5xx for a non-idempotent op only when the server never ran it", async () => {
    for (const [status, code, posts] of [
      [502, "", 1],
      [503, "OVERLOADED", 1],
      [503, "AUTH_UNAVAILABLE", 2],
    ] as const) {
      const { t, calls } = harness([
        { status, body: { detail: { code, message: "x" } } },
        { status: 200, body: {} },
      ]);
      const call = t.post("memory.set_from_messages", {});
      if (posts === 1) await expect(call).rejects.toBeInstanceOf(BackendUnavailable);
      else await call;
      expect(calls.length).toBe(posts);
    }
    const { t, calls } = harness([{ status: 502 }, { status: 200, body: {} }]);
    await t.post("memory.get", {});
    expect(calls.length).toBe(2);
  });

  it("retries a timeout for an op the server dedups, never for another one, always when told", async () => {
    const a = harness([
      { status: 0, throw: ABORT },
      { status: 200, body: { ok: 1 } },
    ]);
    expect(await a.t.post("memory.set", {}, { timeoutMs: 5 })).toEqual({ ok: 1 });
    expect(a.calls.length).toBe(2);

    // Read-only: no replay, so a retry would run a second synthesis beside the first.
    const ro = harness([{ status: 0, throw: ABORT }]);
    await expect(ro.t.post("memory.ask", {}, { timeoutMs: 5 })).rejects.toBeInstanceOf(TimeoutError);
    expect(ro.calls.length).toBe(1);
    expect(a.calls[1].headers["X-Loom-Retry-Count"]).toBe("1");

    const b = harness([{ status: 0, throw: ABORT }]);
    await expect(b.t.post("memory.set_from_messages", {}, { timeoutMs: 5 })).rejects.toBeInstanceOf(TimeoutError);
    expect(b.calls.length).toBe(1);

    const c = harness(
      [
        { status: 0, throw: ABORT },
        { status: 200, body: {} },
      ],
      { retry: { retryTimeouts: "always" } },
    );
    await c.t.post("memory.set_from_messages", {}, { timeoutMs: 5 });
    expect(c.calls.length).toBe(2);

    const d = harness([{ status: 0, throw: ABORT }], { retry: { retryTimeouts: "never" } });
    await expect(d.t.post("memory.get", {}, { timeoutMs: 5 })).rejects.toBeInstanceOf(TimeoutError);
    expect(d.calls.length).toBe(1);
  });

  it("a total backoff budget stops retrying before the attempt count does", async () => {
    const { t, calls, sleeps } = harness(
      [{ status: 500 }, { status: 500 }, { status: 500 }, { status: 200, body: {} }],
      {
        retry: { maxAttempts: 4, backoffMs: 1000, backoffJitter: 0, totalBackoffBudgetMs: 3500 },
      },
    );
    await expect(t.post("memory.get", {})).rejects.toBeInstanceOf(BackendUnavailable);
    expect(sleeps).toEqual([1000, 2000]); // a third sleep of 4000 would exceed the 3500 ms budget (3000 spent) → give up
    expect(calls.length).toBe(3);
  });

  it("retries only the statuses in the policy and 408 by default", async () => {
    const a = harness([{ status: 408 }, { status: 200, body: {} }]);
    await a.t.post("memory.get", {});
    expect(a.calls.length).toBe(2);
    const b = harness([{ status: 500 }], { retry: { statuses: [502] } });
    await expect(b.t.post("memory.get", {})).rejects.toBeInstanceOf(BackendUnavailable);
    expect(b.calls.length).toBe(1);
  });

  it("does not retry a connection error when told not to", async () => {
    const { t, calls } = harness([{ status: 0, throw: new TypeError("fetch failed") }], {
      retry: { retryConnectionErrors: false },
    });
    await expect(t.post("memory.get", {})).rejects.toBeInstanceOf(BackendUnavailable);
    expect(calls.length).toBe(1);
  });
});
