import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearToken,
  getMySharingSummary,
  listServiceAccounts,
  onSignInRejected,
  post,
  setToken,
  setPageOwner,
  signOut,
  signedOut,
  subscribeInflight,
} from "./api";
import { ApiError } from "./lib/errors";

// The GET reads ride the same request path as post(): a failure is an ApiError with the
// server's code and request id, every request carries a signal (the caller's or the 60 s
// bound), and the inflight counter sees it.

function stubFetch(status: number, body: string, headers: Record<string, string> = {}) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(body, { status, headers }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  const err: unknown = await p.then(
    () => undefined,
    (e: unknown) => e,
  );
  if (!(err instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(err)}`);
  return err;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const failure = '{"detail":{"code":"INTERNAL","message":"sharing rollup failed"}}';

describe("getMySharingSummary", () => {
  it("rejects a 500 with an ApiError carrying the code and request id", async () => {
    const fetchMock = stubFetch(500, failure, { "X-Request-Id": "req-s" });
    const err = await rejection(getMySharingSummary());
    expect(err.status).toBe(500);
    expect(err.code).toBe("INTERNAL");
    expect(err.requestId).toBe("req-s");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/me/sharing-summary");
    expect(init?.method).toBe("GET");
    expect(init?.body).toBeUndefined();
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("passes the caller's signal through", async () => {
    const fetchMock = stubFetch(200, "{}");
    const ctrl = new AbortController();
    await getMySharingSummary({ signal: ctrl.signal });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(ctrl.signal);
  });
});

describe("listServiceAccounts", () => {
  it("rejects a 500 with an ApiError carrying the code and request id, under a bounded signal", async () => {
    const fetchMock = stubFetch(500, failure, { "X-Request-Id": "req-sa" });
    const err = await rejection(listServiceAccounts());
    expect(err.code).toBe("INTERNAL");
    expect(err.requestId).toBe("req-sa");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/me/service_accounts");
    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });

  it("answers the rows and counts toward the inflight indicator while pending", async () => {
    let release: (r: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            release = resolve;
          }),
      ),
    );
    const seen: number[] = [];
    const unsubscribe = subscribeInflight((n) => seen.push(n));
    const pending = listServiceAccounts();
    expect(seen.at(-1)).toBe(1);
    release(new Response('{"rows":[{"name":"ci"}]}', { status: 200 }));
    await expect(pending).resolves.toEqual([{ name: "ci" }]);
    expect(seen.at(-1)).toBe(0);
    unsubscribe();
  });
});

/** A token whose claims name `sub` in org o (boot.ts reads them; the signature is not). */
function jwtFor(sub: string, iat = 1): string {
  const part = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
  return `${part({ alg: "HS256" })}.${part({ sub, org: "o", iat })}.sig`;
}
const ALICE_1 = jwtFor("alice", 1);
const ALICE_2 = jwtFor("alice", 2);

describe("a refused sign-in token", () => {
  afterEach(() => {
    localStorage.clear();
    onSignInRejected(() => undefined);
    setPageOwner(null);
  });

  it("ends the session on a 401 for the stored token, and still rejects the call", async () => {
    const ended = vi.fn(() => clearToken());
    onSignInRejected(ended);
    setToken("tok-a");
    stubFetch(401, '{"detail":{"code":"AUTH_INVALID","message":"token expired: Signature has expired"}}');
    const err = await rejection(post("/v1/collection.list", { org: "o" }));
    expect(err.status).toBe(401);
    expect(err.sessionEnded).toBe(true);
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("ends it once for 401s that were in flight together", async () => {
    const ended = vi.fn(() => clearToken());
    onSignInRejected(ended);
    setToken("tok-a");
    stubFetch(401, "{}");
    const errors = await Promise.all([1, 2, 3].map(() => rejection(post("/v1/memory.list", { org: "o" }))));
    expect(ended).toHaveBeenCalledTimes(1);
    // Each reads as the ended session, not only the first.
    expect(errors.every((e) => e.sessionEnded)).toBe(true);
  });

  it("leaves a 403, and a token stored while the request was in flight, alone", async () => {
    const ended = vi.fn();
    onSignInRejected(ended);
    setToken(ALICE_1);
    stubFetch(403, '{"detail":{"code":"FORBIDDEN","message":"no grant"}}');
    await rejection(post("/v1/collection.list", { org: "o" }));
    const fetchMock = vi.fn(async () => {
      setToken(ALICE_2); // a fresh sign-in finished meanwhile
      return new Response("{}", { status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const err = await rejection(post("/v1/collection.list", { org: "o" }));
    // Not "sign in again": the person already did, elsewhere; Retry sends it.
    expect(err.code).toBe("SIGNED_IN_ELSEWHERE");
    expect(localStorage.getItem("loom_token_v1")).toBe(ALICE_2);
    expect(ended).not.toHaveBeenCalled();
  });

  it("reads a call raced by someone else's token as the ended session, not as a retry", async () => {
    const ended = vi.fn(() => clearToken());
    onSignInRejected(ended);
    setPageOwner({ subject: "alice", org: "o" });
    setToken(ALICE_1);
    const fetchMock = vi.fn(async () => {
      setToken(jwtFor("bob")); // someone else signed in while this call was out
      return new Response("{}", { status: 401 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const err = await rejection(post("/v1/memory.delete", { org: "o" }));
    // Someone else's token: not "you signed in again"; the tab reloads for them.
    expect(err.sessionEnded).toBe(true);
    expect(ended).not.toHaveBeenCalled();
  });

  it("never sends a call under a token that names someone other than the page's person", async () => {
    setPageOwner({ subject: "alice", org: "o" });
    try {
      setToken(jwtFor("bob")); // another tab signed bob in; this tab has not heard yet
      const fetchMock = vi.fn(async () => new Response("{}"));
      vi.stubGlobal("fetch", fetchMock);
      const err = await rejection(post("/v1/memory.set", { org: "o" }));
      expect(fetchMock).not.toHaveBeenCalled();
      expect(err.sessionEnded).toBe(true);
      setToken(ALICE_2);
      await post("/v1/memory.set", { org: "o" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      setPageOwner(null);
    }
  });

  it("does nothing for a request sent without a stored token", async () => {
    const ended = vi.fn();
    onSignInRejected(ended);
    stubFetch(401, "{}");
    await rejection(post("/v1/memory.list", { org: "o" }));
    expect(ended).not.toHaveBeenCalled();
  });
});

describe("signing out", () => {
  afterEach(() => localStorage.clear());

  it("marks the browser signed out until the next sign-in", () => {
    setToken("tok-a");
    signOut();
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
    expect(signedOut()).toBe(true);
    setToken("tok-b");
    expect(signedOut()).toBe(false);
  });
});

describe("with auth off", () => {
  afterEach(() => {
    delete window.__LOOM_AUTH_DISABLED;
    localStorage.clear();
    vi.resetModules();
  });

  it("sends every call a stale token got refused again without it", async () => {
    window.__LOOM_AUTH_DISABLED = true;
    vi.resetModules();
    const api = await import("./api"); // reads the flag as it loads
    api.onSignInRejected(() => api.clearToken());
    // As App does: auth-off boot's stand-in identity is the page's person.
    api.setPageOwner({ subject: "local", org: "dev" });
    api.setToken(jwtFor("alice"));
    const sentWith: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        const auth = new Headers(init?.headers).get("Authorization") ?? "";
        sentWith.push(auth);
        return auth ? new Response("{}", { status: 401 }) : new Response('{"ok": true}');
      }),
    );
    const answers = await Promise.all([1, 2, 3].map(() => api.post("/v1/memory.list", { org: "o" })));
    expect(answers).toEqual([{ ok: true }, { ok: true }, { ok: true }]);
    expect(sentWith.filter((a) => a === "")).toHaveLength(3);
  });
});
