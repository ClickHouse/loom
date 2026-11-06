import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  claimsFromToken,
  provisionalNamespace,
  rememberKinds,
  rememberedKinds,
  SIGN_IN_PENDING,
  sameIdentity,
  markSignInStarted,
  sameAccount,
  takeLinkToken,
  takeSignInStarted,
} from "./boot";
import type { MyNamespaceEntry } from "../api";
import { VIEW_LOADERS } from "../views/registry";
import { ALL_VIEWS } from "./state";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) =>
    btoa(String.fromCharCode(...new TextEncoder().encode(s)))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256", typ: "JWT" }))}.${b64(JSON.stringify(payload))}.sig`;
}

describe("boot claims", () => {
  it("names the sign-in session a token belongs to", () => {
    const payload = btoa(JSON.stringify({ sub: "alice", sid: "s1" })).replace(/=+$/, "");
    expect(claimsFromToken(`h.${payload}.s`)?.session).toBe("s1");
  });

  it("reads org, namespaces and subject from the token payload without verifying it", () => {
    const c = claimsFromToken(jwt({ sub: "alice", org: "acme", namespaces: ["team", "code-x"], roles: ["ADMIN"] }));
    expect(c).toEqual({ org: "acme", namespaces: ["team", "code-x"], subject: "alice", session: null });
  });

  it("is null for a missing, malformed or payload-less token", () => {
    expect(claimsFromToken(null)).toBeNull();
    expect(claimsFromToken("")).toBeNull();
    expect(claimsFromToken("not-a-jwt")).toBeNull();
    expect(claimsFromToken("a..c")).toBeNull();
    expect(claimsFromToken("a.!!!.c")).toBeNull();
  });

  it("is null when the payload is JSON but not an object", () => {
    const token = (json: string) => `h.${btoa(json).replace(/=+$/, "")}.sig`;
    expect(claimsFromToken(token("null"))).toBeNull();
    expect(claimsFromToken(token('["acme"]'))).toBeNull();
    expect(claimsFromToken(token("7"))).toBeNull();
  });

  it("tolerates a payload with the wrong shapes", () => {
    const c = claimsFromToken(jwt({ org: 7, namespaces: "team", sub: null }));
    expect(c).toEqual({ org: null, namespaces: [], subject: null, session: null });
  });

  it("decodes non-ASCII claim text", () => {
    const c = claimsFromToken(jwt({ org: "ünïcode", namespaces: ["équipe"] }));
    expect(c?.org).toBe("ünïcode");
    expect(c?.namespaces).toEqual(["équipe"]);
  });
});

describe("provisional namespace", () => {
  const KINDS = { team: "memory", other: "memory", "code-x": "code", qa: "qa" };
  it("is the saved pick when the claims carry it and it is remembered as a memory namespace", () => {
    expect(provisionalNamespace("memories", "team", ["other", "team"], KINDS)).toBe("team");
  });
  it("is the first claim for a memory view when the pick is not claimed", () => {
    expect(provisionalNamespace("memories", "joined-only", ["team"], KINDS)).toBe("team");
  });
  it("waits (null) for a code view whose namespace depends on kinds", () => {
    expect(provisionalNamespace("code", null, ["team"], KINDS)).toBeNull();
  });
  it("waits (null) when the token has no claims", () => {
    expect(provisionalNamespace("memories", "team", [], KINDS)).toBeNull();
  });
  it("waits (null) when the namespace's kind is unknown — a first visit", () => {
    expect(provisionalNamespace("memories", "team", ["team"], {})).toBeNull();
  });
  it("waits (null) when the pick is remembered as a code or tracker namespace", () => {
    expect(provisionalNamespace("memories", "code-x", ["code-x", "team"], KINDS)).toBeNull();
    expect(provisionalNamespace("sharing", "qa", ["qa"], KINDS)).toBeNull();
  });
});

describe("remembered kinds", () => {
  it("round-trips the kinds of the last membership answer through localStorage", () => {
    localStorage.clear();
    expect(rememberedKinds()).toEqual({});
    const row = (namespace: string, kind: MyNamespaceEntry["kind"]): MyNamespaceEntry => ({
      namespace,
      kind,
      role: "viewer",
      visibility: "private",
      description: "",
      joined_at: null,
      is_new: false,
    });
    rememberKinds([row("team", "memory"), row("code-x", "code"), row("", "memory")]);
    expect(rememberedKinds()).toEqual({ team: "memory", "code-x": "code" });
  });
  it("ignores a corrupt entry", () => {
    localStorage.setItem("loom_kinds_v1", "{not json");
    expect(rememberedKinds()).toEqual({});
    localStorage.setItem("loom_kinds_v1", JSON.stringify({ team: 3, ok: "memory" }));
    expect(rememberedKinds()).toEqual({ ok: "memory" });
  });
});

describe("identity equality", () => {
  it("compares the three fields, never the object", () => {
    const a = { org: "acme", namespace: "team", agent: "loom-ui" };
    expect(sameIdentity(a, { ...a })).toBe(true);
    expect(sameIdentity(a, { ...a, namespace: "other" })).toBe(false);
    expect(sameIdentity(null, a)).toBe(false);
  });
});

describe("view registry", () => {
  it("has a loader for every view", () => {
    for (const v of ALL_VIEWS) expect(typeof VIEW_LOADERS[v]).toBe("function");
    expect(Object.keys(VIEW_LOADERS).sort()).toEqual([...ALL_VIEWS].sort());
  });
});

// A login link's token is taken from the #token= fragment, and a ?token= query never is;
// either leaves the address at once, before anything can fail.
describe("login link token", () => {
  afterEach(() => history.replaceState(null, "", "/"));
  const address = () => window.location.pathname + window.location.search + window.location.hash;

  it("takes a #token= fragment and scrubs it from the address bar", () => {
    history.replaceState(null, "", "/setup?tab=a#token=abc.def%2Eghi");
    expect(takeLinkToken()).toEqual({ token: "abc.def.ghi", atRoot: false, nonce: null });
    expect(address()).toBe("/setup?tab=a");
  });

  it("never takes a ?token= query, and removes it anyway", () => {
    history.replaceState(null, "", "/?token=leaked.jwt.value&tab=a#memories");
    expect(takeLinkToken()).toEqual({ token: null, atRoot: false, nonce: null });
    expect(window.location.search).toBe("?tab=a");
    expect(window.location.hash).toBe("#memories");
    expect(window.location.href).not.toContain("leaked");
  });

  it("leaves a URL without a token alone", () => {
    history.replaceState(null, "", "/library?q=x#y");
    expect(takeLinkToken()).toBeNull();
    expect(window.location.search + window.location.hash).toBe("?q=x#y");
  });

  it("says when the token came back to the bare root, where a sign-in returns to its page", () => {
    for (const root of ["/", "/me"]) {
      history.replaceState(null, "", `${root}#token=abc`);
      expect(takeLinkToken(), root).toEqual({ token: "abc", atRoot: true, nonce: null });
      expect(address(), root).toBe(root);
    }
    history.replaceState(null, "", "/?tab=a#token=abc");
    expect(takeLinkToken()?.atRoot).toBe(false);
  });

  it("keeps the entry's state as the token leaves the address", () => {
    history.replaceState({ loomPlace: { scrollTop: 40 } }, "", "/memories#token=abc");
    takeLinkToken();
    expect(history.state).toEqual({ loomPlace: { scrollTop: 40 } });
  });

  it("scrubs a fragment that does not decode, and takes no token from it", () => {
    history.replaceState(null, "", "/#token=%E0%A4%A");
    expect(takeLinkToken()).toEqual({ token: null, atRoot: true, nonce: null });
    expect(address()).toBe("/");
  });
});

describe("a sign-in this tab started", () => {
  beforeEach(() => sessionStorage.clear());

  it("trusts only its own nonce, for ten minutes, once", () => {
    const nonce = markSignInStarted(1_000);
    expect(nonce).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(markSignInStarted(1_000)).not.toBe(nonce);
    const again = markSignInStarted(1_000);
    expect(takeSignInStarted(again, 1_000 + 10 * 60 * 1000)).toBe(true);
    expect(takeSignInStarted(again, 1_000 + 1)).toBe(false);
  });

  it("is none for another nonce, no nonce, a mark over ten minutes old, or no mark", () => {
    const nonce = markSignInStarted(1_000);
    expect(takeSignInStarted("someone-elses-nonce-0000", 1_001)).toBe(false);
    expect(sessionStorage.getItem(SIGN_IN_PENDING)).toBeNull(); // used up by a wrong one too
    markSignInStarted(1_000);
    expect(takeSignInStarted(null, 1_001)).toBe(false);
    const late = markSignInStarted(1_000);
    expect(takeSignInStarted(late, 1_000 + 10 * 60 * 1000 + 1)).toBe(false);
    expect(takeSignInStarted(nonce)).toBe(false);
  });

  it("reads the sign-in's nonce from the landing's query, only a well-formed one, and scrubs it", () => {
    history.replaceState(null, "", "/me?n=Ab_-Ab_-Ab_-Ab_-#token=abc.def.ghi");
    expect(takeLinkToken()).toEqual({ token: "abc.def.ghi", atRoot: true, nonce: "Ab_-Ab_-Ab_-Ab_-" });
    expect(window.location.search + window.location.hash).toBe("");
    history.replaceState(null, "", "/me?n=short#token=abc");
    expect(takeLinkToken()).toEqual({ token: "abc", atRoot: true, nonce: null });
    expect(window.location.search + window.location.hash).toBe("");
    // The fragment is the token alone, as a dashboard that predates the nonce reads it.
    history.replaceState(null, "", "/me#token=abc&n=Ab_-Ab_-Ab_-Ab_-");
    expect(takeLinkToken()?.nonce).toBeNull();
    // A page's own `n` stays when no token comes with it.
    history.replaceState(null, "", "/library?n=1&token=leak");
    takeLinkToken();
    expect(window.location.search).toBe("?n=1");
  });

  it("names the same account only for the same subject and org", () => {
    const alice = jwt({ sub: "alice", org: "acme" });
    expect(sameAccount(jwt({ sub: "alice", org: "acme", iat: 2 }), alice)).toBe(true);
    expect(sameAccount(jwt({ sub: "bob", org: "acme" }), alice)).toBe(false);
    expect(sameAccount(jwt({ sub: "alice", org: "other" }), alice)).toBe(false);
    expect(sameAccount(alice, null)).toBe(false);
    expect(sameAccount("garbage", "garbage")).toBe(false);
  });
});
