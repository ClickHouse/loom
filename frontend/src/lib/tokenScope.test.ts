import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { everythingScope, describeTokenScope } from "./tokenScope";

const row = (
  namespace: string,
  kind: MyNamespaceEntry["kind"],
  status: "active" | "pending" = "active",
): MyNamespaceEntry => ({
  namespace,
  role: "viewer",
  status,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind,
});

const repos = [
  { namespace: "code-ch", repo: "org/ch", branch: "master", classification: "standard" },
  { namespace: "code-ch-26-8", repo: "org/ch", branch: "26.8", classification: "standard" },
  { namespace: "code-private", repo: "org/private", branch: "master", classification: "restricted" },
];

describe("everythingScope", () => {
  it("reads every active membership; the home pick only sets where it writes", () => {
    const s = everythingScope(
      [row("me", "memory"), row("team", "memory"), row("code-ch", "code"), row("code-ch-26-8", "code")],
      repos,
      "me",
    );
    expect(s.home).toBe("me");
    expect(s.namespaces).toEqual(["me", "team"]);
    expect(s.repositories).toEqual([
      {
        repo: "org/ch",
        branches: [
          { namespace: "code-ch", branch: "master" },
          { namespace: "code-ch-26-8", branch: "26.8" },
        ],
      },
    ]);
    expect(s.excludedRestricted).toEqual([]);
    expect(s.pending).toEqual([]);
  });

  it("a restricted repository the person belongs to is listed as excluded, never as readable", () => {
    const s = everythingScope([row("me", "memory"), row("code-private", "code")], repos, "me");
    expect(s.repositories).toEqual([]);
    expect(s.excludedRestricted).toEqual([
      { repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] },
    ]);
  });

  it("pending joins are neither readable nor excluded; they are pending", () => {
    const s = everythingScope([row("me", "memory"), row("shared", "memory", "pending")], null, "me");
    expect(s.namespaces).toEqual(["me"]);
    expect(s.pending).toEqual(["shared"]);
  });

  it("the home namespace is always in the read set, even before the list loads", () => {
    const s = everythingScope(null, null, "me");
    expect(s.namespaces).toEqual(["me"]);
    expect(s.repositories).toEqual([]);
  });

  it("a code namespace with no repo facts keeps its identifier and counts as readable", () => {
    const s = everythingScope([row("code-x", "code")], null, "me");
    expect(s.repositories).toEqual([{ repo: "code-x", branches: [{ namespace: "code-x", branch: "code-x" }] }]);
  });
});

describe("describeTokenScope", () => {
  it("a token claiming `*` reads everything and saves to the namespace named first", () => {
    expect(describeTokenScope({ namespaces: ["me", "*"], roles: ["writer"] })).toBe(
      "reads everything you belong to · saves to me",
    );
  });
  it("a bare `*` token saves where its owner works", () => {
    expect(describeTokenScope({ namespaces: ["*"], roles: ["writer"] })).toBe(
      "reads everything you belong to · saves to the namespace you pick in Loom",
    );
  });
  it("a `*` token of someone who writes nowhere is read-only", () => {
    expect(describeTokenScope({ namespaces: ["*"], roles: ["writer"] }, { writesAnywhere: false })).toBe(
      "reads everything you belong to · read-only",
    );
  });
  it("a `*` token minted read-only says so", () => {
    expect(describeTokenScope({ namespaces: ["ns", "*"], roles: ["viewer"] })).toBe(
      "reads everything you belong to · read-only",
    );
  });
  it("a home you can no longer write gives way to your pick", () => {
    const writable = (ns: string) => ns !== "team-x";
    expect(describeTokenScope({ namespaces: ["team-x", "*"], roles: ["writer"] }, { writable })).toBe(
      "reads everything you belong to · saves to the namespace you pick in Loom",
    );
  });
  it("another person's `*` token reads as theirs, with no read-only judgement of yours", () => {
    expect(
      describeTokenScope({ namespaces: ["team-x", "*"], roles: ["writer"] }, { writesAnywhere: false, yours: false }),
    ).toBe("reads everything its owner belongs to · saves to team-x");
    expect(describeTokenScope({ namespaces: ["*"], roles: ["writer"] }, { yours: false })).toBe(
      "reads everything its owner belongs to · saves to the namespace its owner picks in Loom",
    );
  });
  it("names beside `*` are the restricted ones it also reads", () => {
    expect(describeTokenScope({ namespaces: ["me", "*", "code-private"], roles: ["writer"] })).toBe(
      "reads everything you belong to, plus code-private · saves to me",
    );
  });
  it("a listed token reads only its list, each at its role", () => {
    expect(
      describeTokenScope({
        namespaces: ["docs", "billing"],
        roles: ["viewer", "writer"],
        ns_roles: { docs: "viewer", billing: "writer" },
      }),
    ).toBe("reads only docs (viewer), billing (writer)");
  });
  it("a listed token with one role says it once, and read-only when it writes nowhere", () => {
    expect(
      describeTokenScope({ namespaces: ["a", "b"], roles: ["viewer"], ns_roles: { a: "viewer", b: "viewer" } }),
    ).toBe("reads only a, b (viewer) · read-only");
  });
  it("a flat token for one namespace reads only it", () => {
    expect(describeTokenScope({ namespaces: ["me"], roles: ["writer"] })).toBe("reads only me");
  });
  it("names a repository by repository and branch, not by its namespace", () => {
    const label = (n: string) => (n === "code-secret" ? "org/secret · main" : n);
    expect(describeTokenScope({ namespaces: ["me", "*", "code-secret"], roles: ["writer"] }, { label })).toBe(
      "reads everything you belong to, plus org/secret · main · saves to me",
    );
    expect(
      describeTokenScope(
        { namespaces: ["code-secret"], roles: ["viewer"], ns_roles: { "code-secret": "viewer" } },
        { label },
      ),
    ).toBe("reads only org/secret · main (viewer) · read-only");
  });
  it("a token with no claim reads nothing", () => {
    expect(describeTokenScope({ namespaces: [], roles: ["writer"] })).toBe("reads nothing");
  });
});
