import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearToken,
  getMySharingSummary,
  listServiceAccounts,
  onSignInRejected,
  post,
  setToken,
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

describe("a refused sign-in token", () => {
  afterEach(() => {
    localStorage.clear();
    onSignInRejected(() => undefined);
  });

  it("ends the session on a 401 for the stored token, and still rejects the call", async () => {
    const ended = vi.fn(() => clearToken());
    onSignInRejected(ended);
    setToken("tok-a");
    stubFetch(401, '{"detail":{"code":"AUTH_INVALID","message":"token expired: Signature has expired"}}');
    const err = await rejection(post("/v1/collection.list", { org: "o" }));
    expect(err.status).toBe(401);
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("ends it once for 401s that were in flight together", async () => {
    const ended = vi.fn(() => clearToken());
    onSignInRejected(ended);
    setToken("tok-a");
    stubFetch(401, "{}");
    await Promise.all([1, 2, 3].map(() => rejection(post("/v1/memory.list", { org: "o" }))));
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("leaves a 403 and a token stored while the request was in flight alone", async () => {
    const ended = vi.fn();
    onSignInRejected(ended);
    setToken("tok-a");
    stubFetch(403, '{"detail":{"code":"FORBIDDEN","message":"no grant"}}');
    await rejection(post("/v1/collection.list", { org: "o" }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        setToken("tok-b"); // a fresh sign-in finished meanwhile
        return new Response("{}", { status: 401 });
      }),
    );
    await rejection(post("/v1/collection.list", { org: "o" }));
    expect(ended).not.toHaveBeenCalled();
  });

  it("does nothing for a request sent without a stored token", async () => {
    const ended = vi.fn();
    onSignInRejected(ended);
    stubFetch(401, "{}");
    await rejection(post("/v1/memory.list", { org: "o" }));
    expect(ended).not.toHaveBeenCalled();
  });
});
