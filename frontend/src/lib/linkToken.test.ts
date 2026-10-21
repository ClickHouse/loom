import { afterEach, describe, expect, it, vi } from "vitest";
import { SIGN_IN_FROM, rememberSignInStart, takeLinkToken } from "./linkToken";

// A login link's token is taken from the #token= fragment and scrubbed from the address; a
// ?token= query never is. Any token's arrival ends the sign-in it may come from, and one
// that lands on the root returns to the page that sign-in started on: within ten minutes,
// on this origin.

const address = () => window.location.pathname + window.location.search + window.location.hash;
const startedAt = (path: string, at = Date.now()) => sessionStorage.setItem(SIGN_IN_FROM, JSON.stringify({ path, at }));

describe("login link token", () => {
  afterEach(() => {
    history.replaceState(null, "", "/");
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("takes a #token= fragment and scrubs it from the address bar", () => {
    history.replaceState(null, "", "/setup?tab=a#token=abc.def%2Eghi");
    expect(takeLinkToken()).toBe("abc.def.ghi");
    expect(address()).toBe("/setup?tab=a");
  });

  it("never takes a ?token= query, and removes it anyway", () => {
    history.replaceState(null, "", "/?token=leaked.jwt.value&tab=a#memories");
    expect(takeLinkToken()).toBeNull();
    expect(window.location.search).toBe("?tab=a");
    expect(window.location.hash).toBe("#memories");
    expect(window.location.href).not.toContain("leaked");
  });

  it("leaves a URL without a token alone", () => {
    history.replaceState(null, "", "/library?q=x#y");
    expect(takeLinkToken()).toBeNull();
    expect(window.location.search + window.location.hash).toBe("?q=x#y");
  });

  it("a token back on the root opens the page its sign-in started on, once", () => {
    for (const root of ["/", "/me"]) {
      startedAt("/memories?namespace=group-a-shared&memory=k");
      history.replaceState(null, "", `${root}#token=abc`);
      expect(takeLinkToken(), root).toBe("abc");
      expect(address(), root).toBe("/memories?namespace=group-a-shared&memory=k");
      expect(sessionStorage.getItem(SIGN_IN_FROM), root).toBeNull();
    }
  });

  it("returns to no start older than ten minutes", () => {
    startedAt("/memories?namespace=group-a-shared", Date.now() - 11 * 60 * 1000);
    history.replaceState(null, "", "/#token=abc");
    expect(takeLinkToken()).toBe("abc");
    expect(address()).toBe("/");
  });

  it("any token's arrival ends the sign-in: one that names its own page, and a refused query", () => {
    startedAt("/memories");
    history.replaceState(null, "", "/settings?tab=memory#token=abc");
    expect(takeLinkToken()).toBe("abc");
    expect(window.location.pathname + window.location.search).toBe("/settings?tab=memory");
    expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
    startedAt("/memories");
    history.replaceState(null, "", "/?token=leaked");
    expect(takeLinkToken()).toBeNull();
    expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
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
      history.replaceState(null, "", "/#token=abc");
      expect(takeLinkToken(), JSON.stringify(from)).toBe("abc");
      expect(address(), JSON.stringify(from)).toBe("/");
    }
    sessionStorage.setItem(SIGN_IN_FROM, "not json");
    history.replaceState(null, "", "/#token=abc");
    expect(takeLinkToken()).toBe("abc");
    expect(address()).toBe("/");
  });

  it("scrubs the token even when the address cannot be written to the start", () => {
    startedAt("/memories");
    history.replaceState(null, "", "/#token=abc");
    const replace = history.replaceState.bind(history);
    vi.spyOn(history, "replaceState")
      .mockImplementationOnce(() => {
        throw new DOMException("refused", "SecurityError");
      })
      .mockImplementation((data, unused, url) => replace(data, unused, url));
    expect(takeLinkToken()).toBe("abc");
    expect(address()).toBe("/");
  });

  it("the gate remembers the page a sign-in starts on, and when; from the root, nothing", () => {
    history.replaceState(null, "", "/memories?namespace=group-a-shared");
    rememberSignInStart();
    const kept = JSON.parse(sessionStorage.getItem(SIGN_IN_FROM) ?? "{}");
    expect(kept.path).toBe("/memories?namespace=group-a-shared");
    expect(Math.abs(Date.now() - kept.at)).toBeLessThan(5000);
    history.replaceState(null, "", "/");
    rememberSignInStart();
    expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
  });
});
