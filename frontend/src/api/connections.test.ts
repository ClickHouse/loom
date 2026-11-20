import { afterEach, describe, expect, it, vi } from "vitest";
import { clearToken, setPageOwner, setToken } from "../api";
import { ApiError } from "../lib/errors";
import { disconnectConnection, listConnections, startConnection } from "./connections";

afterEach(() => {
  clearToken();
  setPageOwner(null);
  vi.unstubAllGlobals();
});

describe("connected account API", () => {
  it("uses the own-account ops and authenticated start route with abort signals", async () => {
    setPageOwner(null);
    setToken("personal-test-bearer");
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    await listConnections("acme", { signal: controller.signal });
    await disconnectConnection("acme", "notion", { signal: controller.signal });
    await startConnection("linear", { signal: controller.signal });
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/v1/connection.list",
      "/v1/connection.disconnect",
      "/v1/connect/linear/start",
    ]);
    const bodies = fetch.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies[0]).toMatchObject({ org: "acme" });
    expect(bodies[1]).toMatchObject({ org: "acme", kind: "notion" });
    for (const body of bodies) {
      expect(body).not.toHaveProperty("namespace");
      expect(body).not.toHaveProperty("subject");
      expect(body).not.toHaveProperty("credential");
    }
    for (const [, init] of fetch.mock.calls) {
      expect(init?.method).toBe("POST");
      expect(init?.headers).toMatchObject({ Authorization: "Bearer personal-test-bearer" });
      expect(init?.signal).toBe(controller.signal);
    }
    controller.abort();
    expect(fetch.mock.calls.every(([, init]) => init?.signal?.aborted)).toBe(true);
  });

  it("keeps the shared transport's structured refusal and request id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ detail: { code: "AUTH_INSUFFICIENT", message: "a primary sign-in session is required" } }),
            { status: 403, headers: { "X-Request-Id": "connect-refusal" } },
          ),
      ),
    );
    await expect(startConnection("gdrive")).rejects.toMatchObject({
      status: 403,
      code: "AUTH_INSUFFICIENT",
      requestId: "connect-refusal",
    } satisfies Partial<ApiError>);
  });
});
