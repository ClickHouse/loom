import { afterEach, describe, expect, it, vi } from "vitest";
import { invalidateShared, shared } from "./sharedReads";
import { setPageOwner, setToken } from "../api";

afterEach(() => {
  invalidateShared();
  vi.useRealTimers();
});

describe("shared", () => {
  it("two askers within the TTL share one request; after the TTL a new one is issued", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(async () => 42);
    const a = shared("k", 1000, fetcher);
    const b = shared("k", 1000, fetcher);
    expect(a).toBe(b);
    expect(await a).toBe(42);
    expect(fetcher).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1500);
    await shared("k", 1000, fetcher);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("a rejected read is forgotten so the next asker retries", async () => {
    let n = 0;
    const fetcher = vi.fn(async () => {
      n += 1;
      if (n === 1) throw new Error("offline");
      return "ok";
    });
    await expect(shared("k", 60_000, fetcher)).rejects.toThrow("offline");
    expect(await shared("k", 60_000, fetcher)).toBe("ok");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("invalidateShared by prefix forgets only matching keys", async () => {
    const f = vi.fn(async () => 1);
    await shared("mine:acme", 60_000, f);
    await shared("repos:acme", 60_000, f);
    invalidateShared("mine:");
    await shared("mine:acme", 60_000, f);
    await shared("repos:acme", 60_000, f);
    expect(f).toHaveBeenCalledTimes(3);
  });
});

describe("sharedHealth", () => {
  it("bounds the request with an AbortSignal and forgets a rejected read", async () => {
    const { sharedHealth } = await import("./sharedReads");
    const inits: (RequestInit | undefined)[] = [];
    let n = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        inits.push(init);
        n += 1;
        if (n === 1) throw new DOMException("The operation timed out.", "TimeoutError");
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }),
    );
    try {
      await expect(sharedHealth()).rejects.toThrow("timed out");
      expect(await sharedHealth()).toEqual({ status: "ok" });
      expect(inits).toHaveLength(2);
      for (const init of inits) expect(init?.signal).toBeInstanceOf(AbortSignal);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("sends the page owner's token, and no other person's", async () => {
    const { sharedHealth } = await import("./sharedReads");
    const part = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    const token = (sub: string) => `${part({ alg: "HS256" })}.${part({ sub, org: "o" })}.sig`;
    const sent: (string | null)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        sent.push(new Headers(init?.headers).get("Authorization"));
        return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
      }),
    );
    setPageOwner({ authenticated: true, subject: "alice", org: "o" });
    try {
      setToken(token("alice"));
      await sharedHealth();
      invalidateShared();
      setToken(token("bob"));
      await sharedHealth();
      expect(sent).toEqual([`Bearer ${token("alice")}`, null]);
    } finally {
      setPageOwner(null);
      localStorage.clear();
      vi.unstubAllGlobals();
    }
  });

  it("answers null for a failure status or a body that is not an object", async () => {
    const { sharedHealth } = await import("./sharedReads");
    const bodies = [new Response("{}", { status: 503 }), new Response("[1]", { status: 200 })];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bodies.shift() ?? new Response("null", { status: 200 })),
    );
    try {
      expect(await sharedHealth()).toBeNull();
      invalidateShared();
      expect(await sharedHealth()).toBeNull();
      invalidateShared();
      expect(await sharedHealth()).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("sharedTagVocabulary", () => {
  it("reads the taxonomy's tags, not a 500-row memory.list", async () => {
    const { sharedTagVocabulary } = await import("./sharedReads");
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        posts.push(url.replace(/^https?:\/\/[^/]+/, "").split("?")[0] + " " + String(init?.body ?? ""));
        return new Response(
          JSON.stringify({
            tags: [
              { value: "runbook", count: 3 },
              { value: "alerts", count: 9 },
            ],
            entities: [],
            memory_types: [],
            memory_count: 12,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );
    try {
      const tags = await sharedTagVocabulary({ org: "acme", namespace: "vocab-ns", agent: "loom-ui" });
      expect(tags).toEqual(["alerts", "runbook"]);
      expect(posts).toHaveLength(1);
      expect(posts[0]).toContain("/v1/namespace.taxonomy");
      expect(posts[0]).toContain('"top_n_tags":500');
      expect(posts[0]).toContain('"top_n_communities":0');
      expect(posts.some((p) => p.startsWith("/v1/memory.list"))).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
