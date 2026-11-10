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
  it("reads every active membership, repositories grouped by repository", () => {
    const s = everythingScope(
      [row("me", "memory"), row("team", "memory"), row("code-ch", "code"), row("code-ch-26-8", "code")],
      repos,
    );
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
    const s = everythingScope([row("me", "memory"), row("code-private", "code")], repos);
    expect(s.repositories).toEqual([]);
    expect(s.excludedRestricted).toEqual([
      { repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] },
    ]);
  });

  it("pending joins are neither readable nor excluded; they are pending", () => {
    const s = everythingScope([row("me", "memory"), row("shared", "memory", "pending")], null);
    expect(s.namespaces).toEqual(["me"]);
    expect(s.pending).toEqual(["shared"]);
  });

  it("a code namespace with no repo facts keeps its identifier and counts as readable", () => {
    const s = everythingScope([row("code-x", "code")], null);
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
  it("a `*` token of someone who writes nowhere is read-only, and of one who writes only a collection saves nowhere by default", () => {
    expect(describeTokenScope({ namespaces: ["*"], roles: ["writer"] }, { writable: [] })).toBe(
      "reads everything you belong to · read-only",
    );
    // A collection a call names is still written: not read-only.
    expect(
      describeTokenScope({ namespaces: ["*"], roles: ["writer"] }, { writable: [], writesAny: ["col-team"] }),
    ).toBe("reads everything you belong to · saves nowhere by default");
  });
  it("a `*` token minted read-only says so", () => {
    expect(describeTokenScope({ namespaces: ["ns", "*"], roles: ["viewer"] })).toBe(
      "reads everything you belong to · read-only",
    );
  });
  it("a home you can no longer write gives way to your pick", () => {
    const writable = ["me"];
    expect(describeTokenScope({ namespaces: ["team-x", "*"], roles: ["writer"] }, { writable })).toBe(
      "reads everything you belong to · saves to the namespace you pick in Loom",
    );
  });
  it("another person's `*` token reads as theirs, with no read-only judgement of yours", () => {
    expect(describeTokenScope({ namespaces: ["team-x", "*"], roles: ["writer"] }, { writable: [], yours: false })).toBe(
      "reads everything its owner belongs to · saves to team-x",
    );
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
  it("a listed token names where its mint recorded it saves, and says when it can no longer write there", () => {
    const t = { namespaces: ["docs", "mine"], roles: ["viewer", "writer"], saves_to: "mine" };
    expect(describeTokenScope({ ...t, ns_roles: { docs: "viewer", mine: "writer" } })).toBe(
      "reads only docs (viewer), mine (writer) · saves to mine",
    );
    // Its owner lost write there since the mint: the token writes through the owner's grants,
    // so its calls land in the next namespace both may write, or nowhere.
    const roles = { ns_roles: { docs: "writer", mine: "writer" } };
    expect(describeTokenScope({ ...t, ...roles }, { writable: ["docs"] })).toBe(
      "reads only docs, mine (writer) · saves to docs now; can't write to mine",
    );
    expect(describeTokenScope({ ...t, ns_roles: { docs: "viewer", mine: "writer" } }, { writable: ["docs"] })).toBe(
      "reads only docs (viewer), mine (writer) · read-only now; can't write to mine",
    );
    // Its owner still writes a listed collection, which a call may name.
    expect(
      describeTokenScope(
        {
          ...t,
          namespaces: ["docs", "mine", "col-team"],
          ns_roles: { docs: "viewer", mine: "writer", "col-team": "writer" },
        },
        { writable: ["docs"], writesAny: ["docs", "col-team"] },
      ),
    ).toBe(
      "reads only docs (viewer), mine (writer), col-team (writer) · saves nowhere by default now; can't write to mine",
    );
    expect(describeTokenScope({ ...t, ...roles }, { writable: ["mine"] })).toBe(
      "reads only docs, mine (writer) · saves to mine",
    );
    // Minted before tokens recorded it: where it saves can follow its owner's pick, so the
    // line names no namespace.
    expect(
      describeTokenScope({ namespaces: ["docs"], roles: ["writer"], ns_roles: { docs: "writer" } }, { yours: false }),
    ).toBe("reads only docs (writer)");
  });
  it("a `*` token whose home is a collection follows its owner's pick: a collection is no token's home", () => {
    // The page's writable list leaves collections out (Tokens.tsx useWritable), as the server does.
    expect(describeTokenScope({ namespaces: ["col-team", "*"], roles: ["writer"] }, { writable: ["team"] })).toBe(
      "reads everything you belong to · saves to the namespace you pick in Loom",
    );
    expect(describeTokenScope({ namespaces: ["col-team", "*"], roles: ["writer"] }, { yours: false })).toBe(
      "reads everything its owner belongs to · saves to the namespace its owner picks in Loom",
    );
  });
  it("a token with no claim reads nothing", () => {
    expect(describeTokenScope({ namespaces: [], roles: ["writer"] })).toBe("reads nothing");
  });
});
