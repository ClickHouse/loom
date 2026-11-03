import { afterEach, describe, expect, it, vi } from "vitest";
import { setToken, signOut } from "../api";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}

describe("sign out", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("forgets the token at once and never waits for the server", () => {
    const fetch = vi.fn(() => new Promise<Response>(() => {})); // a request that never answers
    vi.stubGlobal("fetch", fetch);
    setToken(jwt({ sub: "alice", sid: "s1" }));
    expect(signOut()).toBe(true); // returns at once, having signed this person out
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.keepalive).toBe(true);
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it("only forgets a pasted token that is not a sign-in session's", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    setToken(jwt({ sub: "ci" }));
    signOut();
    expect(fetch).not.toHaveBeenCalled();
    expect(localStorage.getItem("loom_token_v1")).toBeNull();
  });
});
