import { describe, expect, it } from "vitest";
import type { CodeRepoSummary } from "../api";
import {
  buildCards,
  buildCodeFacet,
  buildRecordFacet,
  canBeApproved,
  maintainsOutOfScope,
  primaryAction,
  soleMembership,
  standingPhrase,
  verbForState,
  visibleBranches,
} from "./repositoryCard";
import type { TrackerSource, TrackerSourceNamespace } from "./trackerSources";
import { must } from "../test/must";

// One card per repository, two facets. The join key between a tracker namespace and a
// repository card is the namespace's `repos[]`.

function branch(over: Partial<CodeRepoSummary> = {}): CodeRepoSummary {
  return {
    org: "dev",
    namespace: "code-clickhouse",
    repo: "ClickHouse/ClickHouse",
    branch: "master",
    git_head: "abc123def456",
    clone_head: "abc123def456",
    fast_tier_lag: 0,
    scip_lag: 0,
    scip_py_lag: 0,
    indexed_at: "2026-09-16 12:00:00",
    last_refresh_at: "",
    counts: { code_files: 31539, code_symbols: 400000 },
    join_policy: "open",
    my_status: "none",
    my_role: "",
    source: "config",
    ...over,
  };
}

function ns(over: Partial<TrackerSourceNamespace> = {}): TrackerSourceNamespace {
  return {
    namespace: "tracker-clickhouse",
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
    origin: "config",
    host: "qa.example",
    port: 9440,
    database: "qa_intelligence",
    configured: true,
    contract: { ok: true, summary: "", checked_at: "2026-09-16T17:27:00Z", optional_present: [], features: {} },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: { issues: 22583, prs: 65334, last_refresh_at: "2026-09-16 17:24:59" },
    namespaces: [
      ns(),
      ns({ namespace: "tracker-example-private", classification: "restricted", join_policy: "approval" }),
    ],
    ...over,
  };
}

const TEN = Array.from({ length: 10 }, (_, i) =>
  branch({ namespace: `code-ch-${i}`, branch: i === 0 ? "master" : `25.${i}` }),
);

describe("verbForState: one verb per state, the same five words on both facets", () => {
  it("maps member → Open, open → Join, approval → Request access, pending → Withdraw request, closed/registering → nothing", () => {
    expect(verbForState("member")).toBe("Open");
    expect(verbForState("open")).toBe("Join");
    expect(verbForState("approval")).toBe("Request access");
    expect(verbForState("pending")).toBe("Withdraw request");
    expect(verbForState("closed")).toBeNull();
    expect(verbForState("registering")).toBeNull();
  });
});

describe("buildCards joins code.repos and tracker.source.list by repository", () => {
  it("one card for ClickHouse/ClickHouse with both facets; the private namespace joins it too because its repos name it", () => {
    const cards = buildCards([branch()], [source()]);
    expect(cards).toHaveLength(1);
    const c = cards[0];
    expect(c.repo).toBe("ClickHouse/ClickHouse");
    expect(c.code?.branches.map((b) => b.branch)).toEqual(["master"]);
    expect(c.record?.namespaces.map((n) => n.ns.namespace)).toEqual(["tracker-clickhouse", "tracker-example-private"]);
    expect(c.record?.namespaces.map((n) => n.label)).toEqual(["Public", "Private"]);
    expect(c.record?.freshness).toMatchObject({ issues: 22583, prs: 65334 });
    expect(c.restricted).toBe(false);
  });

  it("a namespace naming two repositories sits on both cards", () => {
    const two = ns({ namespace: "qa-both", repos: ["ClickHouse/ClickHouse", "ClickHouse/example-private"] });
    const cards = buildCards([branch()], [source({ namespaces: [two] })]);
    expect(cards.map((c) => c.repo).sort()).toEqual(["ClickHouse/ClickHouse", "ClickHouse/example-private"]);
    expect(cards.every((c) => c.record?.namespaces[0].ns.namespace === "qa-both")).toBe(true);
  });

  it("a tracker namespace whose repos match no configured repository gets its own card headed by the repository it declares", () => {
    const cards = buildCards([branch()], [source()]);
    expect(cards.map((c) => c.repo)).toEqual(["ClickHouse/ClickHouse"]);
    const withStranger = buildCards(
      [branch()],
      [source({ namespaces: [ns({ namespace: "tracker-other", repos: ["Acme/widgets"] })] })],
    );
    expect(withStranger.map((c) => c.repo).sort()).toEqual(["Acme/widgets", "ClickHouse/ClickHouse"]);
    const stranger = must(
      withStranger.find((c) => c.repo === "Acme/widgets"),
      "Acme/widgets card",
    );
    expect(stranger.code).toBeNull();
    expect(stranger.record?.namespaces[0].ns.namespace).toBe("tracker-other");
    expect(stranger.standing).toBe("not joined");
  });

  it("a namespace declaring no repository is headed by its source's name; the join is case-insensitive", () => {
    const cards = buildCards(
      [branch({ repo: "clickhouse/clickhouse" })],
      [source({ namespaces: [ns(), ns({ namespace: "tracker-loose", repos: [] })] })],
    );
    expect(cards.map((c) => c.repo).sort()).toEqual(["clickhouse/clickhouse", "qa"]);
    expect(cards.find((c) => c.repo === "clickhouse/clickhouse")?.record?.namespaces).toHaveLength(1);
  });

  it("a code-only repository has no record facet; a QA-less deployment yields code cards only", () => {
    const cards = buildCards([branch()], []);
    expect(cards[0].record).toBeNull();
    expect(cards[0].code?.branches).toHaveLength(1);
  });

  it("sorts repositories you read first, then joinable ones, then by name", () => {
    const cards = buildCards(
      [
        branch({ repo: "B/read", namespace: "b", my_status: "member" }),
        branch({ repo: "A/restricted", namespace: "a", join_policy: "approval" }),
        branch({ repo: "C/open", namespace: "c" }),
      ],
      [],
    );
    expect(cards.map((c) => c.repo)).toEqual(["B/read", "C/open", "A/restricted"]);
  });

  it("restricted when every facet namespace needs approval", () => {
    const locked = buildCards(
      [branch({ join_policy: "approval" })],
      [source({ namespaces: [ns({ classification: "restricted", join_policy: "approval" })] })],
    );
    expect(locked[0].restricted).toBe(true);
    const mixed = buildCards([branch({ join_policy: "approval" })], [source({ namespaces: [ns()] })]);
    expect(mixed[0].restricted).toBe(false);
  });

  it("a member of a restricted repository still sees it as restricted", () => {
    // the standing-based reading said "open" to the maintainer; the server's class decides
    const member = buildCards(
      [branch({ join_policy: "approval", classification: "restricted", my_status: "member", my_role: "admin" })],
      [],
    );
    expect(member[0].restricted).toBe(true);
    const openMember = buildCards(
      [branch({ join_policy: "open", classification: "standard", my_status: "member" })],
      [],
    );
    expect(openMember[0].restricted).toBe(false);
    // without the field, approval alone is not restricted
    const noClass = buildCards([branch({ join_policy: "approval", my_status: "member" })], []);
    expect(noClass[0].restricted).toBe(false);
  });

  it("maintained when the caller is admin on a branch or on a record namespace", () => {
    expect(buildCards([branch({ my_role: "admin", my_status: "member" })], [])[0].maintained).toBe(true);
    expect(buildCards([branch()], [source({ namespaces: [ns({ member_role: "admin" })] })])[0].maintained).toBe(true);
    expect(buildCards([branch()], [source()])[0].maintained).toBe(false);
  });
});

describe("standingPhrase: the caller's standing in one phrase", () => {
  it("'you read 8 of 10 branches · tracker: not joined'", () => {
    const rows = TEN.map((b, i) => (i < 8 ? { ...b, my_status: "member" as const } : b));
    const [c] = buildCards(rows, [source()]);
    expect(c.standing).toBe("you read 8 of 10 branches · tracker: not joined");
  });
  it("nothing joined anywhere is 'not joined'", () => {
    expect(buildCards([branch()], [source()])[0].standing).toBe("not joined");
    expect(buildCards([branch()], [])[0].standing).toBe("not joined");
  });
  it("a pending branch and a read record", () => {
    const [c] = buildCards(
      [branch({ my_status: "pending", join_policy: "approval" })],
      [source({ namespaces: [ns({ member_role: "viewer" })] })],
    );
    expect(c.standing).toBe("1 branch requested · tracker: reading");
  });
  it("code read, record requested", () => {
    const [c] = buildCards(
      [branch({ my_status: "member" })],
      [source({ namespaces: [ns({ pending_role: "viewer", join_policy: "approval" })] })],
    );
    expect(c.standing).toBe("you read 1 of 1 branch · tracker: requested");
  });
  it("a record-only card says only its record", () => {
    expect(
      standingPhrase({
        code: null,
        record: buildRecordFacet([{ ns: ns({ member_role: "viewer" }), source: source() }]),
      }),
    ).toBe("tracker: reading");
  });
});

describe("primaryAction: exactly one primary button", () => {
  it("Open the branch you read, before anything else", () => {
    const [c] = buildCards([branch({ my_status: "member" })], [source()]);
    expect(primaryAction(c)).toMatchObject({ verb: "Open", facet: "code", qualifier: "master" });
  });
  it("Open the record when only the record is read", () => {
    const [c] = buildCards(
      [branch({ join_policy: "approval" })],
      [source({ namespaces: [ns({ member_role: "viewer" })] })],
    );
    expect(primaryAction(c)).toMatchObject({ verb: "Open", facet: "record", qualifier: "tracker" });
  });
  it("Join the open branch first; the record's Join when the code is restricted; Request access when nothing is open", () => {
    expect(primaryAction(buildCards([branch()], [source()])[0])).toMatchObject({
      verb: "Join",
      facet: "code",
      qualifier: "master",
    });
    expect(primaryAction(buildCards([branch({ join_policy: "approval" })], [source()])[0])).toMatchObject({
      verb: "Join",
      facet: "record",
    });
    expect(
      primaryAction(
        buildCards(
          [branch({ join_policy: "approval" })],
          [source({ namespaces: [ns({ classification: "restricted", join_policy: "approval" })] })],
        )[0],
      ),
    ).toMatchObject({ verb: "Request access", facet: "code" });
  });
  it("Withdraw request when the only thing left is pending; nothing on a closed repository", () => {
    expect(primaryAction(buildCards([branch({ my_status: "pending", join_policy: "approval" })], [])[0])).toMatchObject(
      { verb: "Withdraw request" },
    );
    expect(primaryAction(buildCards([branch({ join_policy: "closed" })], [])[0])).toBeNull();
  });
  it("the primary branch (master) wins over another branch in the same state", () => {
    const [c] = buildCards(
      [branch({ namespace: "a", branch: "25.8" }), branch({ namespace: "b", branch: "master" })],
      [],
    );
    expect(primaryAction(c)?.branch?.branch).toBe("master");
  });
});

describe("soleMembership: the header's one quiet Leave", () => {
  it("is the one thing read; null with none or several", () => {
    expect(soleMembership(buildCards([branch({ my_status: "member" })], [source()])[0])).toMatchObject({
      facet: "code",
      qualifier: "master",
    });
    expect(
      soleMembership(buildCards([branch()], [source({ namespaces: [ns({ member_role: "viewer" })] })])[0]),
    ).toMatchObject({ facet: "record" });
    expect(
      soleMembership(
        buildCards([branch({ my_status: "member" })], [source({ namespaces: [ns({ member_role: "viewer" })] })])[0],
      ),
    ).toBeNull();
    expect(soleMembership(buildCards([branch()], [source()])[0])).toBeNull();
  });
});

describe("the Code facet keeps the 6830 group shape and hides the long tail of branches", () => {
  it("master first, release branches newest first; primary is master; head is the first read branch", () => {
    const f = buildCodeFacet(
      "ClickHouse/ClickHouse",
      [...TEN].reverse().map((b, i) => (i === 3 ? { ...b, my_status: "member" as const } : b)),
    );
    expect(f.branches[0].branch).toBe("master");
    expect(f.branches[1].branch).toBe("25.9");
    expect(f.primary.branch).toBe("master");
    expect(f.head.branch).toBe("25.6");
    expect(f.joinedCount).toBe(1);
    expect(f.joinable).toHaveLength(9);
  });
  it("visibleBranches shows the trunk, what you read or requested, and fills to four; expanded shows all", () => {
    const rows = TEN.map((b, i) =>
      i === 7 ? { ...b, my_status: "member" as const } : i === 9 ? { ...b, my_status: "pending" as const } : b,
    );
    const f = buildCodeFacet("r", rows);
    expect(visibleBranches(f, false).map((b) => b.branch)).toEqual(["master", "25.9", "25.8", "25.7"]);
    expect(visibleBranches(f, true)).toHaveLength(10);
    expect(visibleBranches(buildCodeFacet("r", TEN.slice(0, 3)), false)).toHaveLength(3);
  });
});

describe("the record facet", () => {
  it("labels by scope, public first; falls back to the namespace name when two share a scope", () => {
    const f = buildRecordFacet([
      { ns: ns({ namespace: "tracker-p", classification: "restricted", join_policy: "approval" }), source: source() },
      { ns: ns({ namespace: "tracker-a" }), source: source() },
      { ns: ns({ namespace: "tracker-b" }), source: source() },
    ]);
    expect(f.namespaces.map((n) => n.label)).toEqual(["tracker-a", "tracker-b", "Private"]);
  });
  it("unconfigured when every source lacks a credential; freshness from the first source with counts", () => {
    const dry = source({
      configured: false,
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
    });
    expect(buildRecordFacet([{ ns: ns(), source: dry }]).unconfigured).toBe(true);
    expect(buildRecordFacet([{ ns: ns(), source: dry }]).freshness).toBeNull();
    expect(
      buildRecordFacet([
        { ns: ns(), source: dry },
        { ns: ns({ namespace: "x" }), source: source() },
      ]).freshness,
    ).toMatchObject({ issues: 22583 });
  });
  it("neither list op says who can approve before the click: canBeApproved is unknown, so Request stays enabled", () => {
    expect(canBeApproved(ns())).toBeNull();
  });
});

describe("maintaining out of scope (`unreadable_reason`)", () => {
  it("maintainsOutOfScope: an admin whose token does not name the repository is told to sign in with one, not offered adopt", () => {
    expect(maintainsOutOfScope({ unreadable_reason: "token_scope", my_role: "admin" })).toBe(true);
    expect(maintainsOutOfScope({ unreadable_reason: "token_scope", my_role: "viewer" })).toBe(false);
    expect(maintainsOutOfScope({ unreadable_reason: "not_member", my_role: "admin" })).toBe(false);
    expect(maintainsOutOfScope({ my_role: "admin" })).toBe(false);
  });
});
