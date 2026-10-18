import { describe, expect, it } from "vitest";
import type { TrackerItem } from "../../../api";
import {
  areaClass,
  areaDocFrom,
  buildBoard,
  cellIssues,
  duplicateFrom,
  emptyFacets,
  filterBoard,
  heat,
  issuesForCell,
  neighbourNumbers,
  patternsAvailable,
  trackerGraphFrom,
  restrictedSections,
  severityFacets,
  trendGlyph,
} from "./model";
import { must } from "../../../test/must";

// The board is derived from `severity_board` cells and the `test_gaps` snapshot; the
// facets narrow it client-side; the mode exists only when the binding names a tracker
// namespace; the tracker graph is the selected issue's caused_by / fixed_by / duplicate
// edges.

const BOARD_ROWS = [
  { component: "MergeTree", severity: "medium", open: 27, untested: 20, flaky: 1, bisected: 8 },
  { component: "MergeTree", severity: "low", open: 206, untested: 178, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "critical", open: 2, untested: 2, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "low", open: 40, untested: 30, flaky: 0, bisected: 3 },
  { component: "", severity: "medium", open: 2, untested: 2, flaky: 0, bisected: 0 },
  { component: "Ghost", severity: "low", open: 0, untested: 0, flaky: 0, bisected: 0 },
];
const GAP_ROWS = [
  {
    component: "MergeTree",
    total_bugs: 191,
    bugs_without_test: 137,
    gap_ratio: 0.7173,
    risk_score: 396.7,
    trend_delta: 0.2729,
    open_bugs: 59,
    critical_bugs: 141,
    snapshot_date: "2026-09-16",
  },
  {
    component: "Keeper",
    total_bugs: 50,
    bugs_without_test: 10,
    gap_ratio: 0.2,
    risk_score: 12,
    trend_delta: -0.1,
    open_bugs: 42,
    critical_bugs: 2,
    snapshot_date: "2026-09-16",
  },
];

describe("buildBoard: cells and snapshot rows become the matrix", () => {
  it("sorts components by open count, orders severities by rank, and joins the gap snapshot", () => {
    const b = buildBoard(BOARD_ROWS, GAP_ROWS);
    expect(b.rows.map((r) => r.label)).toEqual(["MergeTree", "Keeper", "(no component)"]);
    expect(b.severities).toEqual(["critical", "medium", "low"]);
    expect(b.maxOpen).toBe(206);
    expect(b.totalOpen).toBe(277);
    const mt = b.rows[0];
    expect(mt.open).toBe(233);
    expect(mt.untested).toBe(198);
    expect(mt.cells.get("medium")).toMatchObject({ open: 27, untested: 20, flaky: 1, bisected: 8 });
    expect(mt.gap?.gap_ratio).toBeCloseTo(0.7173);
    expect(mt.gap?.trend_delta).toBeCloseTo(0.2729);
    expect(b.rows[2].gap).toBeNull();
  });

  it("drops zero cells and a snapshot row with no open issue is not a row", () => {
    const b = buildBoard(BOARD_ROWS, [
      ...GAP_ROWS,
      { component: "Quiet", gap_ratio: 0.9, trend_delta: 0, total_bugs: 3, bugs_without_test: 3 },
    ]);
    expect(b.rows.find((r) => r.component === "Ghost")).toBeUndefined();
    expect(b.rows.find((r) => r.component === "Quiet")).toBeUndefined();
  });

  it("is empty for an empty answer", () => {
    const b = buildBoard([], []);
    expect(b.rows).toEqual([]);
    expect(b.severities).toEqual([]);
    expect(b.maxOpen).toBe(0);
  });
});

describe("filterBoard: the facets narrow rows, columns and flagged cells", () => {
  const full = buildBoard(BOARD_ROWS, GAP_ROWS);

  it("keeps only the chosen components and recomputes the scale", () => {
    const f = { ...emptyFacets(), components: new Set(["Keeper"]) };
    const b = filterBoard(full, f);
    expect(b.rows.map((r) => r.component)).toEqual(["Keeper"]);
    expect(b.maxOpen).toBe(40);
    expect(b.severities).toEqual(["critical", "low"]);
  });

  it("keeps only the chosen severities", () => {
    const b = filterBoard(full, { ...emptyFacets(), severities: new Set(["medium"]) });
    expect(b.rows.map((r) => r.component)).toEqual(["MergeTree", ""]);
    expect(b.rows[0].open).toBe(27);
  });

  it("a flag keeps the cells with at least one such issue, and drops rows left without a cell", () => {
    const b = filterBoard(full, { ...emptyFacets(), flags: new Set(["bisected" as const]) });
    expect(b.rows.map((r) => r.component)).toEqual(["Keeper", "MergeTree"]);
    expect([
      ...must(
        b.rows.find((r) => r.component === "MergeTree"),
        "MergeTree row",
      ).cells.keys(),
    ]).toEqual(["medium"]);
    const both = filterBoard(full, { ...emptyFacets(), flags: new Set(["bisected" as const, "flaky" as const]) });
    expect(both.rows.map((r) => r.component)).toEqual(["MergeTree"]);
  });

  it("severity facets carry the open counts across the board", () => {
    expect(severityFacets(full)).toEqual([
      { severity: "critical", open: 2 },
      { severity: "medium", open: 29 },
      { severity: "low", open: 246 },
    ]);
  });

  it("the heat is a square-root share so ones stay visible against hundreds", () => {
    expect(heat(206, 206)).toBe(1);
    expect(heat(2, 206)).toBeGreaterThan(0.09);
    expect(heat(0, 206)).toBe(0);
    expect(trendGlyph(0.27)).toEqual({ glyph: "▲", word: "worse" });
    expect(trendGlyph(-0.1).word).toBe("better");
    expect(trendGlyph(0).word).toBe("flat");
  });
});

describe("patternsAvailable: one rule, shown only on a named, authorized tracker namespace", () => {
  it("is false for kind none, an empty name, unauthorized, a malformed or missing answer", () => {
    expect(
      patternsAvailable({ tracker_namespace: "", restricted: false, kind: "none", authorized: false, templates: [] }),
    ).toBe(false);
    expect(
      patternsAvailable({
        tracker_namespace: "tracker-x",
        restricted: false,
        kind: "code",
        authorized: false,
        templates: [],
      }),
    ).toBe(false);
    // A wrong-shape answer. Typed as `object` because the weak Partial<TrackerBinding>
    // rejects an object that shares none of its keys, even through a variable.
    const wrongShape: object = { rows: [] };
    expect(patternsAvailable(wrongShape)).toBe(false);
    expect(patternsAvailable(null)).toBe(false);
    expect(patternsAvailable(undefined)).toBe(false);
  });
  it("is true for a bound code namespace and for a tracker namespace itself", () => {
    expect(
      patternsAvailable({
        tracker_namespace: "tracker-x",
        restricted: false,
        kind: "code",
        authorized: true,
        templates: ["severity_board"],
      }),
    ).toBe(true);
    expect(
      patternsAvailable({
        tracker_namespace: "tracker-x",
        restricted: true,
        kind: "tracker",
        authorized: true,
        templates: [],
      }),
    ).toBe(true);
  });
});

describe("issuesForCell: the drill-down narrows to the cell's severity and the flags", () => {
  const rows = [
    {
      number: 1,
      title: "a",
      severity_level: "medium",
      labels: ["bug"],
      primary_component: "MergeTree",
      has_test: -1,
      is_flaky_test: 0,
      bisect_outcome: "",
      url: "u1",
      state: "open",
    },
    {
      number: 2,
      title: "b",
      severity_level: "low",
      labels: [],
      primary_component: "MergeTree",
      has_test: 1,
      is_flaky_test: 1,
      bisect_outcome: "",
      url: "u2",
      state: "open",
    },
    {
      number: 3,
      title: "c",
      severity_level: "medium",
      labels: [],
      primary_component: "MergeTree",
      has_test: null,
      is_flaky_test: 0,
      bisect_outcome: "reproduced",
      url: "u3",
      state: "open",
    },
  ];
  it("keeps the severity and reads has_test -1 / null as untested", () => {
    const out = issuesForCell(rows, "medium", new Set());
    expect(out.map((i) => i.number)).toEqual([1, 3]);
    expect(out[0].untested).toBe(true);
    expect(out[1].bisected).toBe(true);
  });
  it("a flag narrows further; a tested issue is not untested", () => {
    expect(issuesForCell(rows, "medium", new Set(["bisected" as const])).map((i) => i.number)).toEqual([3]);
    expect(issuesForCell(rows, "low", new Set(["untested" as const]))).toEqual([]);
    expect(issuesForCell(rows, null, new Set(["flaky" as const])).map((i) => i.number)).toEqual([2]);
  });
  it("restricted sections are named, not errors", () => {
    expect(
      restrictedSections([
        { template: "area_incidents", title: "", params: {}, status: "restricted_required", rows: [] },
        { template: "area_open_issues", title: "", params: {}, status: "served", rows: [] },
      ]),
    ).toEqual(["area_incidents"]);
  });
});

describe("trackerGraphFrom: the selected issue's typed, directed neighbourhood", () => {
  const item = (
    number: number,
    kind: "issue" | "pr",
    tracker: TrackerItem["tracker"],
    title = `t${number}`,
  ): TrackerItem => ({
    number,
    kind,
    state: "open",
    is_merged: false,
    title,
    author: "",
    opened_at: "",
    closed_at: "",
    url: `https://x/${number}`,
    tracker,
    line: "",
  });

  it("draws caused_by to the seed as the named culprit and fixed_by as fixes", () => {
    const items = new Map<number, TrackerItem>([
      [
        97288,
        item(97288, "issue", {
          kind: "issue",
          severity: "medium",
          caused_by: [90220],
          fixed_by: [97289, 97304, 97306, 98708],
        }),
      ],
      [90220, item(90220, "pr", { kind: "pr" }, "Show schema for parameterized_view")],
    ]);
    const g = must(trackerGraphFrom(97288, items), "graph of 97288");
    expect(g.seed.kind).toBe("issue");
    expect(g.nodes.map((n) => n.number)).toEqual([90220, 97289, 97304, 97306, 98708]);
    const cause = must(
      g.nodes.find((n) => n.number === 90220),
      "node 90220",
    );
    expect(cause).toMatchObject({
      kind: "pr",
      role: "cause",
      namedByGraph: true,
      known: true,
      title: "Show schema for parameterized_view",
    });
    expect(g.nodes.find((n) => n.number === 97289)).toMatchObject({ kind: "pr", role: "fix", known: false });
    expect(g.edges.find((e) => e.rel === "caused_by")).toMatchObject({ from: "90220", to: "97288", label: "caused" });
    expect(g.edges.filter((e) => e.rel === "fixed_by").map((e) => e.from)).toEqual([
      "97289",
      "97304",
      "97306",
      "98708",
    ]);
    expect(neighbourNumbers(must(items.get(97288), "item 97288"))).toEqual([90220, 97289, 97304, 97306, 98708]);
  });

  it("adds duplicates below, directed by the row's direction; related rows are linked, never promoted to duplicates", () => {
    const items = new Map<number, TrackerItem>([[1, item(1, "issue", { kind: "issue" })]]);
    const dups = [
      duplicateFrom({
        source_id: "ClickHouse/ClickHouse#2",
        title: "dup",
        severity: "low",
        url: "",
        rel_type: "duplicate_of",
        direction: "in",
      }),
      duplicateFrom({
        source_id: "ClickHouse/ClickHouse#3",
        title: "orig",
        severity: "low",
        url: "",
        rel_type: "duplicate_of",
        direction: "out",
      }),
      // 119601 → 116490 is `related_to`
      duplicateFrom({
        source_id: "ClickHouse/ClickHouse#4",
        title: "rel",
        severity: "low",
        url: "",
        rel_type: "related_to",
        direction: "out",
      }),
      duplicateFrom({
        source_id: "ClickHouse/ClickHouse#5",
        title: "other",
        severity: "low",
        url: "",
        rel_type: "reproduces",
        direction: "out",
      }),
    ];
    const g = must(trackerGraphFrom(1, items, dups), "graph of 1");
    expect(g.nodes.map((n) => [n.number, n.role])).toEqual([
      [2, "duplicate"],
      [3, "duplicate"],
      [4, "linked"],
    ]);
    expect(g.edges.map((e) => [e.from, e.to, e.rel, e.label])).toEqual([
      ["2", "1", "duplicate_of", "duplicate of"],
      ["1", "3", "duplicate_of", "duplicate of"],
      ["1", "4", "related_to", "related to"],
    ]);
  });

  it("is null until the seed's record is known, and a PR seed reads fixes / caused outward", () => {
    expect(trackerGraphFrom(5, new Map())).toBeNull();
    const items = new Map<number, TrackerItem>([[9, item(9, "pr", { kind: "pr", fixes: [7], caused: [8] })]]);
    const g = must(trackerGraphFrom(9, items), "graph of 9");
    expect(g.edges.map((e) => [e.rel, e.from, e.to])).toEqual([
      ["caused", "9", "8"],
      ["fixes", "9", "7"],
    ]);
  });
});

describe("cellIssues: a server-filtered read is the cell, not narrowed again", () => {
  // rows as the server returns them for `severities: ["low"]`: every row is
  // low; an answer without the parameter (a severity the server does not rank)
  // is the component's top 50
  const low = (n: number) => ({
    number: n,
    title: `l${n}`,
    severity_level: "low",
    labels: [],
    primary_component: "MergeTree",
    has_test: n % 2 ? -1 : 1,
    is_flaky_test: 0,
    bisect_outcome: "",
    url: "",
    state: "open",
  });
  const rows = [low(1), low(2), low(3)];

  it("keeps every row when params.severities names the cell and reports the ceiling", () => {
    const r = cellIssues(
      { params: { component: "MergeTree", severities: ["low"], limit: 300 }, rows },
      "low",
      new Set(),
    );
    expect(r.serverFiltered).toBe(true);
    expect(r.read).toBe(300);
    expect(r.capped).toBe(false);
    expect(r.issues.map((i) => i.number)).toEqual([1, 2, 3]);
    // the flag chips still narrow client-side
    expect(
      cellIssues(
        { params: { severities: ["low"], limit: 300 }, rows },
        "low",
        new Set(["untested" as const]),
      ).issues.map((i) => i.number),
    ).toEqual([1, 3]);
  });

  it("is capped when the rows reach the limit", () => {
    const r = cellIssues({ params: { severities: ["low"], limit: 3 }, rows }, "low", new Set());
    expect(r.capped).toBe(true);
    expect(r.read).toBe(3);
  });

  it("narrows by severity itself when the server did not rank it, reading limit 50 by default", () => {
    const mixed = [...rows, { ...low(9), severity_level: "medium" }];
    const r = cellIssues({ params: { component: "MergeTree", limit: 50 }, rows: mixed }, "medium", new Set());
    expect(r.serverFiltered).toBe(false);
    expect(r.read).toBe(50);
    expect(r.issues.map((i) => i.number)).toEqual([9]);
    expect(cellIssues({ params: {}, rows: mixed }, "low", new Set()).issues).toHaveLength(3);
    expect(cellIssues(null, "low", new Set())).toEqual({ issues: [], serverFiltered: false, read: 50, capped: false });
  });

  it("another severity in params is not this cell's filter", () => {
    const r = cellIssues({ params: { severities: ["medium"], limit: 300 }, rows }, "low", new Set());
    expect(r.serverFiltered).toBe(false);
    expect(r.issues).toHaveLength(3);
  });
});

describe("areaClass: the restricted class as rows, an honest empty, or the restricted line", () => {
  const incident = {
    source: "core_incident",
    source_id: "acme/incidents#9",
    url: "https://x/9",
    title: "Logical error in loadDataParts",
    severity: "critical",
    component: "MergeTree",
    created_at: "2026-09-01T00:00:00",
    closed_at: null,
    open: true,
    has_test: false,
    error_type: "LOGICAL_ERROR",
    signal: "",
  };
  const escalation = {
    source: "support_case",
    source_id: "acme/support#27722",
    url: "",
    title: "Parts detached after upgrade",
    severity: "medium",
    component: "DataPlane",
    created_at: "2026-08-20T10:00:00",
    closed_at: "2026-08-22T00:00:00",
    open: false,
    org_name: "Acme",
    spoken_name: "prod-eu-1",
    version_affected: "26.4",
  };

  it("maps a row to one line's worth: ref, title, severity, opened, detail", () => {
    expect(areaDocFrom(incident)).toEqual({
      source_id: incident.source_id,
      ref: "#9",
      title: incident.title,
      severity: "critical",
      opened_at: "2026-09-01T00:00:00",
      open: true,
      url: "https://x/9",
      detail: "LOGICAL_ERROR",
    });
    const e = areaDocFrom(escalation);
    expect(e.ref).toBe("#27722");
    expect(e.open).toBe(false);
    expect(e.detail).toBe("customer Acme · instance prod-eu-1 · on 26.4");
  });

  it("reads the section's status and window", () => {
    const sections = [
      {
        template: "area_incidents",
        title: "",
        params: { component: "MergeTree", days: 90 },
        status: "served",
        rows: [incident],
      },
      {
        template: "area_escalations",
        title: "",
        params: { component: "MergeTree", days: 365 },
        status: "empty",
        rows: [],
      },
      { template: "area_open_issues", title: "", params: {}, status: "served", rows: [] },
    ];
    expect(areaClass(sections, "area_incidents")).toMatchObject({ status: "served", days: 90, capped: false });
    // the read's ceiling reached: the list says there may be more
    expect(
      areaClass(
        [{ template: "area_incidents", title: "", params: { days: 90, limit: 1 }, status: "served", rows: [incident] }],
        "area_incidents",
      ).capped,
    ).toBe(true);
    expect(areaClass(sections, "area_incidents").docs[0].ref).toBe("#9");
    expect(areaClass(sections, "area_escalations")).toEqual({
      template: "area_escalations",
      status: "empty",
      docs: [],
      days: 365,
      capped: false,
    });
    expect(areaClass(sections, "customers_in_area").status).toBe("unplanned");
    expect(
      areaClass(
        [{ template: "area_incidents", title: "", params: {}, status: "restricted_required", rows: [] }],
        "area_incidents",
      ),
    ).toMatchObject({ status: "restricted_required", days: 90 });
    expect(
      areaClass(
        [{ template: "area_incidents", title: "", params: {}, status: "tracker_unavailable", rows: [] }],
        "area_incidents",
      ).status,
    ).toBe("tracker_unavailable");
  });
});
