import { afterEach, describe, expect, it } from "vitest";
import {
  claimsFromToken,
  provisionalNamespace,
  rememberKinds,
  rememberedKinds,
  sameIdentity,
  takeLinkToken,
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

describe("login link token", () => {
  afterEach(() => history.replaceState(null, "", "/"));

  it("takes a #token= fragment and scrubs it from the address bar", () => {
    history.replaceState(null, "", "/me/setup?tab=a#token=abc.def%2Eghi");
    expect(takeLinkToken()).toBe("abc.def.ghi");
    expect(window.location.pathname + window.location.search + window.location.hash).toBe("/me/setup?tab=a");
  });

  it("never takes a ?token= query, and removes it anyway", () => {
    history.replaceState(null, "", "/me?token=leaked.jwt.value&tab=a#memories");
    expect(takeLinkToken()).toBeNull();
    expect(window.location.search).toBe("?tab=a");
    expect(window.location.hash).toBe("#memories");
    expect(window.location.href).not.toContain("leaked");
  });

  it("leaves a URL without a token alone", () => {
    history.replaceState(null, "", "/me/library?q=x#y");
    expect(takeLinkToken()).toBeNull();
    expect(window.location.search + window.location.hash).toBe("?q=x#y");
  });
});
