import { afterEach, describe, expect, it, vi } from "vitest";
import { SIGN_IN_FROM } from "./boot";
import { returnToSignInStart } from "./linkToken";

// A token's arrival ends the sign-in it may come from: where it started is read and
// cleared, and a token that came back to the root returns to that page — within ten
// minutes, on this origin, without control characters.

const address = () => window.location.pathname + window.location.search + window.location.hash;
const startedAt = (path: string, at = Date.now()) => sessionStorage.setItem(SIGN_IN_FROM, JSON.stringify({ path, at }));

describe("the return to the page a sign-in started on", () => {
  afterEach(() => {
    history.replaceState(null, "", "/");
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("returns there from the root, once", () => {
    startedAt("/memories?namespace=group-a-shared&memory=k");
    returnToSignInStart(true);
    expect(address()).toBe("/memories?namespace=group-a-shared&memory=k");
    expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
  });

  it("clears the start without returning when the token did not come back to the root", () => {
    history.replaceState(null, "", "/settings?tab=memory");
    startedAt("/memories");
    returnToSignInStart(false);
    expect(address()).toBe("/settings?tab=memory");
    expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
  });

  it("returns to no start older than ten minutes", () => {
    startedAt("/memories?namespace=group-a-shared", Date.now() - 11 * 60 * 1000);
    returnToSignInStart(true);
    expect(address()).toBe("/");
  });

  it("returns only to a path on this origin, without control characters", () => {
    for (const from of [
      "//elsewhere.example/x",
      "/\\elsewhere.example",
      "https://elsewhere.example/",
      "memories",
      "/memories\u0007",
      "/memories\nx",
    ]) {
      startedAt(from);
      returnToSignInStart(true);
      expect(address(), JSON.stringify(from)).toBe("/");
    }
    for (const kept of ["not json", "null", JSON.stringify({ path: 7, at: Date.now() })]) {
      sessionStorage.setItem(SIGN_IN_FROM, kept);
      returnToSignInStart(true);
      expect(address(), kept).toBe("/");
      expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
    }
  });

  it("leaves the address as it is when it cannot be rewritten", () => {
    startedAt("/memories");
    vi.spyOn(history, "replaceState").mockImplementationOnce(() => {
      throw new DOMException("refused", "SecurityError");
    });
    expect(() => returnToSignInStart(true)).not.toThrow();
    expect(address()).toBe("/");
  });
});
