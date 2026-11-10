import { describe, expect, it } from "vitest";
import type { MyNamespaceEntry } from "../api";
import {
  canReadNamespace,
  deepLinkNamespace,
  isMemoryNamespace,
  isPersonalNamespace,
  isSelectableNamespace,
  landingNamespace,
  landingTarget,
  needsMemoryNamespace,
  reachableNamespaces,
  referenceNamespaces,
  WILDCARD_NAMESPACE,
} from "./namespace";

// The landing rule: saved pick if reachable → the user's own namespace → the first real
// claim → nothing. `*` is never a name and a code index is never a memory identity.

function entry(namespace: string, over: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry {
  return {
    namespace,
    role: "viewer",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    ...over,
  };
}

function admin(namespace: string, over: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry {
  return entry(namespace, { role: "admin", status: "active", joined_at: "2026-01-01T00:00:00Z", ...over });
}

describe("landingNamespace: the saved pick wins when it is still reachable", () => {
  it("prefers the saved pick over the claim order and over an own namespace", () => {
    const mine = [admin("personal-alice"), entry("team-b")];
    expect(landingNamespace("team-b", ["bot-corpus", "team-b"], mine)).toBe("team-b");
  });

  it("accepts a saved pick reachable only through list_mine (joined via Discover, not in the JWT)", () => {
    expect(landingNamespace("joined", ["claimed"], [entry("joined")])).toBe("joined");
  });

  it("ignores a saved pick the user can no longer reach", () => {
    expect(landingNamespace("gone", ["team-b"], [])).toBe("team-b");
  });
});

describe("landingNamespace: a benchmark namespace is a default only when nothing else is reachable", () => {
  it("passes over a bench namespace the user administers for a real one", () => {
    const mine = [admin("bench", { kind: "bench" }), entry("team-b")];
    expect(landingNamespace(null, ["bench", "team-b"], mine)).toBe("team-b");
  });

  it("lands on the bench namespace when it is all the token reaches", () => {
    expect(landingNamespace(null, ["bench"], [admin("bench", { kind: "bench" })])).toBe("bench");
  });

  it("keeps a saved pick of a bench namespace", () => {
    const mine = [admin("bench", { kind: "bench" }), entry("team-b")];
    expect(landingNamespace("bench", ["bench", "team-b"], mine)).toBe("bench");
  });
});

describe("landingNamespace: the user's own namespace comes before the first claim", () => {
  it("lands on the admin namespace rather than the claim listed first", () => {
    const mine = [admin("personal-alice")];
    expect(landingNamespace(null, ["shared-public", "personal-alice"], mine)).toBe("personal-alice");
  });

  it("prefers the server's personal-* row over an older admin grant", () => {
    const mine = [
      admin("team-old", { joined_at: "2024-01-01T00:00:00Z" }),
      admin("personal-alice", { joined_at: "2026-06-01T00:00:00Z" }),
    ];
    expect(landingNamespace(null, ["team-old"], mine)).toBe("personal-alice");
  });

  it("otherwise takes the oldest admin grant, with an unknown join date last", () => {
    const mine = [
      admin("team-new", { joined_at: "2026-03-01T00:00:00Z" }),
      admin("no-date", { joined_at: null }),
      admin("team-old", { joined_at: "2025-01-01T00:00:00Z" }),
    ];
    expect(landingNamespace(null, ["team-new"], mine)).toBe("team-old");
  });

  it("does not count a pending admin grant, or a writer/viewer row, as own", () => {
    const mine = [admin("pending-ns", { status: "pending" }), entry("written", { role: "writer" }), entry("readonly")];
    expect(landingNamespace(null, ["readonly", "written"], mine)).toBe("readonly");
  });
});

describe("`*` is never a namespace", () => {
  it("is not selectable and is dropped from the reachable set", () => {
    expect(isSelectableNamespace({ namespace: WILDCARD_NAMESPACE, kind: "memory" })).toBe(false);
    expect(isSelectableNamespace({ namespace: "" })).toBe(false);
    expect([...reachableNamespaces(["*", "", "real"], [])]).toEqual(["real"]);
  });

  it("a wildcard-only claim lands nowhere, and a saved `*` is not honoured", () => {
    expect(landingNamespace(null, ["*"], [])).toBeNull();
    expect(landingNamespace("*", ["*", "real"], [])).toBe("real");
  });

  it("the first real claim wins over the wildcard listed before it", () => {
    expect(landingNamespace(null, ["*", "real", "later"], [])).toBe("real");
  });
});

describe("code indexes and tracker namespaces are never the landing default", () => {
  it("a claimed code index is reachable (a saved pick is honoured) but never landed on by default", () => {
    const mine = [admin("repo-index", { kind: "code" })];
    expect(reachableNamespaces(["repo-index"], mine).has("repo-index")).toBe(true);
    expect(landingNamespace("repo-index", ["repo-index"], mine)).toBe("repo-index");
    expect(landingNamespace(null, ["repo-index"], mine)).toBeNull();
  });

  it("a claimed tracker namespace is reachable but the first memory claim wins the default", () => {
    const mine = [
      entry("tracker-x", { role: "jwt", kind: "tracker" }),
      entry("code-x", { role: "jwt", kind: "code" }),
      entry("public", { role: "jwt", kind: "memory" }),
    ];
    expect(landingNamespace(null, ["tracker-x", "code-x", "public"], mine)).toBe("public");
    expect(landingNamespace("code-x", ["tracker-x", "code-x", "public"], mine)).toBe("code-x");
    expect(isSelectableNamespace({ namespace: "tracker-x", kind: "tracker" })).toBe(false);
  });

  it("an admin code index does not count as the user's own namespace", () => {
    const mine = [admin("repo-index", { kind: "code" }), entry("team")];
    expect(landingNamespace(null, ["team"], mine)).toBe("team");
  });

  it("kind memory, or no kind, is selectable", () => {
    expect(isSelectableNamespace({ namespace: "a", kind: "memory" })).toBe(true);
    expect(isSelectableNamespace({ namespace: "a" })).toBe(true);
  });

  it("a tracker namespace held only through a grant (joined from a tracker source card) is pickable and never the default", () => {
    const mine = [
      admin("personal-alice"),
      entry("tracker-clickhouse", { role: "viewer", status: "active", kind: "tracker" }),
    ];
    expect(reachableNamespaces([], mine).has("tracker-clickhouse")).toBe(true);
    expect(landingNamespace(null, [], mine)).toBe("personal-alice");
    expect(landingNamespace("tracker-clickhouse", [], mine)).toBe("tracker-clickhouse");
  });
});

describe("nothing to land on", () => {
  it("returns null rather than inventing a name", () => {
    expect(landingNamespace(null, [], [])).toBeNull();
    expect(landingNamespace("", [""], [])).toBeNull();
    expect(landingNamespace("saved", [], [])).toBeNull();
  });
});

// A token that names only code and/or QA namespaces lands on Repositories with the picker
// on its first reachable code or tracker namespace; only a token that reaches nothing
// lands on Setup. An explicit path always wins: a deep link keeps its view and takes the
// first reachable namespace of the kind it needs.

const CODE = (ns: string, over: Partial<MyNamespaceEntry> = {}) =>
  entry(ns, { role: "jwt", status: "active", kind: "code", ...over });
const QA = (ns: string, over: Partial<MyNamespaceEntry> = {}) =>
  entry(ns, { role: "jwt", status: "active", kind: "tracker", ...over });

describe("landingTarget: a token that names only repositories lands on Repositories", () => {
  it("code-only: Repositories, picker on the code namespace", () => {
    const mine = [CODE("code-clickhouse")];
    expect(landingTarget(null, ["code-clickhouse"], mine)).toEqual({ view: "code", namespace: "code-clickhouse" });
  });

  it("tracker-only: Repositories, picker on the tracker namespace", () => {
    const mine = [QA("tracker-clickhouse")];
    expect(landingTarget(null, ["tracker-clickhouse"], mine)).toEqual({
      view: "code",
      namespace: "tracker-clickhouse",
    });
  });

  it("code + qa: code before qa, alphabetical within, whatever the claim order", () => {
    const mine = [QA("tracker-clickhouse"), CODE("code-zulu"), CODE("code-alpha")];
    expect(referenceNamespaces(["tracker-clickhouse", "code-zulu", "code-alpha"], mine)).toEqual([
      "code-alpha",
      "code-zulu",
      "tracker-clickhouse",
    ]);
    expect(landingTarget(null, ["tracker-clickhouse", "code-zulu", "code-alpha"], mine)).toEqual({
      view: "code",
      namespace: "code-alpha",
    });
  });

  it("grants-only rows count too (a tracker namespace joined from a source card)", () => {
    const mine = [QA("tracker-clickhouse", { role: "viewer" }), CODE("code-clickhouse", { role: "viewer" })];
    expect(landingTarget(null, [], mine)).toEqual({ view: "code", namespace: "code-clickhouse" });
  });

  it("a pending grant on a code namespace is not reachable yet", () => {
    const mine = [CODE("code-private", { role: "viewer", status: "pending" })];
    expect(referenceNamespaces([], mine)).toEqual([]);
    expect(landingTarget(null, [], mine)).toEqual({ view: "setup", namespace: null });
  });

  it("mixed with a memory claim keeps the 7600 result: the memory namespace, the caller's default view", () => {
    const mine = [CODE("code-clickhouse"), QA("tracker-clickhouse"), entry("public", { role: "jwt", kind: "memory" })];
    expect(landingTarget(null, ["code-clickhouse", "tracker-clickhouse", "public"], mine)).toEqual({
      view: null,
      namespace: "public",
    });
  });

  it("mixed with an own memory namespace keeps landing there", () => {
    const mine = [CODE("code-clickhouse"), admin("personal-alice")];
    expect(landingTarget(null, ["code-clickhouse"], mine)).toEqual({ view: null, namespace: "personal-alice" });
  });

  it("a saved pick wins: a saved code namespace opens Repositories, a saved memory one keeps the default view", () => {
    const mine = [CODE("code-clickhouse"), QA("tracker-clickhouse"), admin("personal-alice")];
    expect(landingTarget("tracker-clickhouse", ["code-clickhouse", "tracker-clickhouse"], mine)).toEqual({
      view: "code",
      namespace: "tracker-clickhouse",
    });
    expect(landingTarget("personal-alice", ["code-clickhouse"], mine)).toEqual({
      view: null,
      namespace: "personal-alice",
    });
    // A stale saved pick falls through to the rule.
    expect(landingTarget("gone", ["code-clickhouse"], [CODE("code-clickhouse")])).toEqual({
      view: "code",
      namespace: "code-clickhouse",
    });
  });

  it("a claim with no list_mine row has no kind and is read as memory, as before", () => {
    expect(landingTarget(null, ["code-clickhouse"], [])).toEqual({ view: null, namespace: "code-clickhouse" });
  });

  it("nothing reachable lands on Setup, and `*` alone is nothing", () => {
    expect(landingTarget(null, [], [])).toEqual({ view: "setup", namespace: null });
    expect(landingTarget(null, ["*"], [])).toEqual({ view: "setup", namespace: null });
  });
});

describe("deepLinkNamespace: an explicit path keeps its view and takes the namespace of the kind it needs", () => {
  const codeTrackerOnly = [CODE("code-clickhouse"), QA("tracker-clickhouse")];
  const claims = ["code-clickhouse", "tracker-clickhouse"];

  it("Repositories and Graph with a code/QA-only token open on the first code namespace", () => {
    expect(deepLinkNamespace("code", null, claims, codeTrackerOnly)).toBe("code-clickhouse");
    expect(deepLinkNamespace("graph", null, claims, codeTrackerOnly)).toBe("code-clickhouse");
    expect(deepLinkNamespace("setup", null, claims, codeTrackerOnly)).toBe("code-clickhouse");
    expect(deepLinkNamespace("browse", null, claims, codeTrackerOnly)).toBe("code-clickhouse");
  });

  it("a memory view with a code/QA-only token gets no namespace — it renders its own empty state", () => {
    for (const v of ["memories", "recall", "profile", "knowledge", "sharing", "sessions", "runs"] as const) {
      expect(needsMemoryNamespace(v)).toBe(true);
      expect(deepLinkNamespace(v, null, claims, codeTrackerOnly)).toBeNull();
    }
    expect(needsMemoryNamespace("code")).toBe(false);
    expect(needsMemoryNamespace("graph")).toBe(false);
    // The assistant's page is its panel, which says itself when the namespace holds no memories.
    expect(needsMemoryNamespace("assistant")).toBe(false);
  });

  it("the 7600 chain still answers first: a memory-only token opens Repositories on its memory namespace", () => {
    expect(deepLinkNamespace("code", null, ["bench"], [admin("bench")])).toBe("bench");
    expect(
      deepLinkNamespace("memories", "tracker-clickhouse", claims, [...codeTrackerOnly, admin("personal-alice")]),
    ).toBe("tracker-clickhouse");
  });
});

describe("isMemoryNamespace: what a memory view may read from", () => {
  it("is false for empty, `*`, code and qa; true for memory and for an unknown kind", () => {
    const mine = [CODE("code-clickhouse"), QA("tracker-clickhouse"), entry("team", { kind: "memory" })];
    expect(isMemoryNamespace("", mine)).toBe(false);
    expect(isMemoryNamespace(WILDCARD_NAMESPACE, mine)).toBe(false);
    expect(isMemoryNamespace("code-clickhouse", mine)).toBe(false);
    expect(isMemoryNamespace("tracker-clickhouse", mine)).toBe(false);
    expect(isMemoryNamespace("team", mine)).toBe(true);
    expect(isMemoryNamespace("unlisted", mine)).toBe(true);
    expect(isMemoryNamespace("anything", null)).toBe(true);
  });
});

describe("canReadNamespace: whether a link's namespace may open", () => {
  it("is true for a claim and for an active grant, whatever its role or kind", () => {
    const mine = [entry("joined", { status: "active" }), CODE("code-clickhouse"), entry("legacy")];
    expect(canReadNamespace("team", ["team"], [])).toBe(true);
    expect(canReadNamespace("joined", ["team"], mine)).toBe(true);
    expect(canReadNamespace("code-clickhouse", ["team"], mine)).toBe(true);
    // A row without a status is an active grant.
    expect(canReadNamespace("legacy", [], mine)).toBe(true);
  });

  it("is false for a namespace the account only asked for, or holds nothing on", () => {
    const mine = [entry("asked", { status: "pending", pending_role: null })];
    expect(canReadNamespace("asked", ["team"], mine)).toBe(false);
    expect(canReadNamespace("group-b-shared", ["team"], mine)).toBe(false);
    // `*` in the claims is no namespace the picker lists.
    expect(canReadNamespace("group-b-shared", [WILDCARD_NAMESPACE], mine)).toBe(false);
  });

  it("counts only the claims when the rows are unknown (list_mine failed)", () => {
    expect(canReadNamespace("group-b-shared", ["team"], null)).toBe(false);
    expect(canReadNamespace("team", ["team"], null)).toBe(true);
  });
});

describe("isPersonalNamespace: the auto-claimed personal-<subject> row", () => {
  it("is true only for the personal- prefix", () => {
    expect(isPersonalNamespace("personal-alice")).toBe(true);
    expect(isPersonalNamespace("personal")).toBe(false);
    expect(isPersonalNamespace("team-personal-x")).toBe(false);
    expect(isPersonalNamespace("")).toBe(false);
  });
});

describe("a collection is never a namespace the header holds", () => {
  const COLLECTION = (namespace: string, over: Partial<MyNamespaceEntry> = {}) =>
    admin(namespace, { kind: "collection", ...over });

  it("is neither selectable nor a memory namespace, by its reserved name whatever its kind", () => {
    const mine = [COLLECTION("col-r"), admin("col-old", { kind: "memory" }), entry("team", { kind: "memory" })];
    for (const name of ["col-r", "col-old", "col-me-0123456789abcdef"]) {
      expect(isSelectableNamespace({ namespace: name, kind: "memory" })).toBe(false);
      expect(isMemoryNamespace(name, mine)).toBe(false);
    }
    expect(isSelectableNamespace({ namespace: "team", kind: "memory" })).toBe(true);
  });

  it("a saved collection, an administered one and a claimed one are passed over", () => {
    const mine = [COLLECTION("col-mine"), admin("team-b", { joined_at: "2026-02-01T00:00:00Z" })];
    expect(landingNamespace("col-mine", ["col-claim", "team-b"], mine)).toBe("team-b");
    expect(landingNamespace(null, ["col-claim"], [COLLECTION("col-mine")])).toBeNull();
  });

  it("an account that reads only collections is Setup's here: the Library's call is landingWithCollections", () => {
    expect(landingTarget(null, [], [COLLECTION("col-a", { role: "viewer" })])).toEqual({
      view: "setup",
      namespace: null,
    });
  });
});
