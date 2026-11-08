import { describe, expect, it } from "vitest";
import { ApiError } from "./errors";
import { joinState } from "./joinActions";
import {
  canAdminister,
  trackerVerbs,
  freshnessOf,
  isOpAbsent,
  mappingProblems,
  namespaceLabel,
  namespaceState,
  operatorOwned,
  sortSources,
  type TrackerSource,
  type TrackerSourceNamespace,
} from "./trackerSources";

// A tracker namespace's standing is the BranchChip rule with `member_role` /
// `pending_role` in place of `my_status`, and a namespace the leader has not registered
// yet is joinable by nobody.

function ns(over: Partial<TrackerSourceNamespace> = {}): TrackerSourceNamespace {
  return {
    namespace: "tracker-x",
    kind: "tracker",
    visibility: "private",
    repos: ["ClickHouse/ClickHouse"],
    classification: "standard",
    join_policy: "open",
    registered: true,
    member_role: "",
    pending_role: "",
    ...over,
  };
}

function source(over: Partial<TrackerSource> = {}): TrackerSource {
  return {
    name: "qa",
    host: "qa.example",
    port: 9440,
    database: "qa_intelligence",
    configured: true,
    contract: { ok: true, summary: "", checked_at: "2026-09-16T17:27:00Z", optional_present: [], features: {} },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: {},
    namespaces: [ns()],
    origin: "config",
    ...over,
  };
}

describe("namespaceState: member / pending / open / approval / closed × classification", () => {
  const roles = ["viewer", "writer", "admin"] as const;
  const classes = ["standard", "restricted"] as const;
  for (const c of classes) {
    for (const r of roles) {
      it(`a ${r} grant on a ${c} namespace is member whatever the rule`, () => {
        for (const p of ["open", "approval", "closed"] as const) {
          expect(namespaceState(ns({ classification: c, member_role: r, join_policy: p }))).toBe("member");
        }
      });
    }
    it(`a pending request on a ${c} namespace is pending, and a member with a pending elevation stays member`, () => {
      expect(namespaceState(ns({ classification: c, pending_role: "viewer", join_policy: "approval" }))).toBe(
        "pending",
      );
      expect(namespaceState(ns({ classification: c, member_role: "viewer", pending_role: "admin" }))).toBe("member");
    });
    it(`no grant on a ${c} namespace follows the rule: open → open, approval → approval, closed → closed`, () => {
      expect(namespaceState(ns({ classification: c, join_policy: "open" }))).toBe("open");
      expect(namespaceState(ns({ classification: c, join_policy: "approval" }))).toBe("approval");
      expect(namespaceState(ns({ classification: c, join_policy: "closed" }))).toBe("closed");
    });
  }
  it("an unregistered namespace is registering, even with a rule that would be open", () => {
    expect(namespaceState(ns({ registered: false, join_policy: "open" }))).toBe("registering");
  });
  it("is the same rule the Repositories page uses for a branch", () => {
    expect(joinState({ member: true, pending: false, join_policy: "closed" })).toBe("member");
    expect(joinState({ member: false, pending: true, join_policy: "open" })).toBe("pending");
    expect(joinState({ member: false, pending: false, join_policy: "open" })).toBe("open");
    expect(joinState({ member: false, pending: false, join_policy: "approval" })).toBe("approval");
    expect(joinState({ member: false, pending: false, join_policy: "closed" })).toBe("closed");
    expect(joinState({ member: false, pending: false })).toBe("approval");
  });
});

describe("sortSources, freshnessOf, namespaceLabel, canAdminister", () => {
  it("sorts sources you read first, then instantly joinable, then by name", () => {
    const read = source({ name: "z-read", namespaces: [ns({ member_role: "viewer" })] });
    const open = source({ name: "m-open", namespaces: [ns()] });
    const locked = source({
      name: "a-locked",
      namespaces: [ns({ join_policy: "approval", classification: "restricted" })],
    });
    expect(sortSources([locked, open, read]).map((s) => s.name)).toEqual(["z-read", "m-open", "a-locked"]);
  });
  it("freshness is the counts when present and null on {}", () => {
    expect(freshnessOf(source())).toBeNull();
    expect(
      freshnessOf(source({ freshness: { issues: 22579, prs: 65328, last_refresh_at: "2026-09-16 17:24:59" } })),
    ).toEqual({ issues: 22579, prs: 65328, last_refresh_at: "2026-09-16 17:24:59" });
  });
  it("labels a namespace by name and repos", () => {
    expect(namespaceLabel(ns())).toBe("tracker-x (ClickHouse/ClickHouse)");
    expect(namespaceLabel(ns({ repos: [] }))).toBe("tracker-x");
  });
  it("only a registry source is not operator-owned", () => {
    expect(operatorOwned(source({ origin: "config" }))).toBe(true);
    expect(operatorOwned(source({ origin: "registry" }))).toBe(false);
  });
  it("the admin menu needs a registry source, an admin grant AND the write ops on the server", () => {
    const s = source({ origin: "registry", namespaces: [ns({ member_role: "admin" })] });
    expect(canAdminister(s, false)).toBe(false);
    expect(canAdminister(s, true)).toBe(true);
    expect(canAdminister(source({ origin: "registry", namespaces: [ns({ member_role: "viewer" })] }), true)).toBe(
      false,
    );
    expect(canAdminister(source({ origin: "config", namespaces: [ns({ member_role: "admin" })] }), true)).toBe(false);
    expect(canAdminister(source({ namespaces: [ns({ member_role: "admin" })] }), true)).toBe(false);
  });
});

describe("trackerVerbs", () => {
  const pub = ns({ namespace: "tracker-pub", member_role: "admin" });
  const priv = ns({ namespace: "tracker-priv", classification: "restricted", member_role: "admin" });
  const writer = { org: "dev", roles: ["writer"] };
  const admin = { org: "dev", roles: ["admin"] };

  it("a writer session holding the grants manages a public tracker", () => {
    expect(trackerVerbs(source({ namespaces: [pub] }), "dev", writer)).toEqual({ manage: true, reason: "" });
  });
  it("a private namespace needs an admin-role token of the org, whatever the grant", () => {
    const mixed = source({ namespaces: [pub, priv] });
    const v = trackerVerbs(mixed, "dev", writer);
    expect(v.manage).toBe(false);
    expect(v.reason).toMatch(/admin-role token of this org/);
    expect(trackerVerbs(mixed, "dev", admin)).toEqual({ manage: true, reason: "" });
    expect(trackerVerbs(mixed, "dev", { org: "other", roles: ["admin"] }).manage).toBe(false);
    expect(trackerVerbs(mixed, "dev", null).manage).toBe(false);
  });
  it("managing needs every namespace, public ones too", () => {
    const notPub = ns({ namespace: "tracker-pub2", member_role: "viewer" });
    for (const token of [admin, writer]) {
      const v = trackerVerbs(source({ namespaces: [pub, notPub, priv] }), "dev", token);
      expect(v.manage).toBe(false);
      expect(v.reason).toMatch(/every namespace/);
    }
  });
});

describe("mappingProblems", () => {
  it("names each mapped table's unmapped core fields in items, PRs, links order, skipping unmapped tables", () => {
    // Links inserted before items: the list still reads in role order.
    const problems = mappingProblems({
      version: 1,
      tables: {
        edges: { source: "links", final: false, columns: { from_ref: "a", to_ref: "b", rel_type: "", from_type: "c" } },
        github_issues: { source: "tickets", final: true, columns: { source_id: "ref", number: "n", title: "t" } },
      },
    });
    expect(problems).toEqual(["Items: map Opened.", "Links: map Relation.", "Links: map To kind."]);
  });
  it("asks for the items table first when it is absent, and nothing for no mapping", () => {
    expect(mappingProblems(null)).toEqual([]);
    expect(mappingProblems({ version: 1, tables: {} })).toEqual(["Pick the table that holds your items."]);
  });
});

describe("isOpAbsent: a server without the tracker-source ops is a stated result", () => {
  it("404 and 405 are absent; an unknown-op code is absent", () => {
    expect(isOpAbsent(new ApiError({ status: 404 }))).toBe(true);
    expect(isOpAbsent(new ApiError({ status: 405 }))).toBe(true);
    expect(isOpAbsent(new ApiError({ status: 400, code: "UNKNOWN_OP" }))).toBe(true);
  });
  it("a refusal, a server error or a network error is not", () => {
    expect(isOpAbsent(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }))).toBe(false);
    expect(isOpAbsent(new ApiError({ status: 500 }))).toBe(false);
    expect(isOpAbsent(new Error("Failed to fetch"))).toBe(false);
  });
});
