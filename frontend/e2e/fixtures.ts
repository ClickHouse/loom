import type { Browser, Locator, Page, TestInfo } from "@playwright/test";

/** A plain JSON object: not null and not an array. Same body as src/lib/guards.ts, which sits
 *  outside the e2e project; request bodies and nested values narrow through this, not a cast. */
export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** The value as a string array, or null when it is not an array of strings. */
export function stringArray(v: unknown): string[] | null {
  return Array.isArray(v) && v.every((x) => typeof x === "string") ? v : null;
}

/** A Click UI Select's trigger: the button its label (or `triggerProps` aria-label) names. */
export function selectTrigger(scope: Page | Locator, name: string | RegExp): Locator {
  return scope.getByRole("button", { name, exact: typeof name === "string" });
}

/** Pick an option in a Click UI Select: open the trigger, click the item by its value (the
 *  items render in a portal and carry `data-value`, not an option role). */
export async function pickOption(scope: Page | Locator, name: string | RegExp, value: string): Promise<void> {
  await selectTrigger(scope, name).click();
  const page = "page" in scope && typeof scope.page === "function" ? scope.page() : (scope as Page);
  await page.locator(`[cui-select-item][data-value="${value}"]`).click();
}

/** The locator's bounding box; an element with no box fails the test by name
 *  instead of a TypeError on `null`. */
export async function boxOf(l: Locator): Promise<NonNullable<Awaited<ReturnType<Locator["boundingBox"]>>>> {
  const b = await l.boundingBox();
  if (b === null) throw new Error(`no bounding box for ${l.toString()}`);
  return b;
}

/**
 * Every `/v1/*` call the dashboard makes is answered here, so the smoke runs
 * without a server and the assertions are about the client alone. The walk
 * is a synthetic 200-node response (12 hop-1 nodes, 12 hop-2 under each, 44 hop-3 to
 * reach the 200-node cap).
 */

export interface TraverseNodeFixture {
  entity: string;
  hop: number;
  cumulative_weight: number;
  via_co_occur: number;
  via_relation: string;
  via_direction: string;
  community_id: number | null;
  path: string[];
}

export interface TraverseFixture {
  seed: string;
  seed_community_id: number | null;
  nodes: TraverseNodeFixture[];
  truncated: boolean;
}

// A list row the read drew, not the loading placeholder (RowSkeleton is a `.list-row` too).
export const LOADED_ROW = "main .list-row:not(.list-row-skeleton)";

/** A 44 px target's least measured size: layout can land a 44 px control at 43.99998 px. */
export const MIN_TARGET_PX = 44 - 0.01;

/** A walk small enough to expand — five hop-1 nodes, no hop 2 — so `facts.related` has
 *  room under the 200-node cap. */
export const SMALL_SEED = "gamma";
export const SMALL_HOP1 = 5;

function walk(seed: string): TraverseFixture {
  const nodes: TraverseNodeFixture[] = [];
  if (seed === SMALL_SEED) {
    for (let i = 0; i < SMALL_HOP1; i++)
      nodes.push({
        entity: `${seed}-h1-${i}`,
        hop: 1,
        cumulative_weight: 0,
        via_co_occur: 20 - i,
        via_relation: "",
        via_direction: "",
        community_id: null,
        path: [seed, `${seed}-h1-${i}`],
      });
    return { seed, seed_community_id: null, nodes, truncated: false };
  }
  const node = (entity: string, hop: number, co: number, path: string[]): TraverseNodeFixture => ({
    entity,
    hop,
    cumulative_weight: 0,
    via_co_occur: co,
    via_relation: "",
    via_direction: "",
    community_id: null,
    path,
  });
  const hop1: string[] = [];
  for (let i = 0; i < 12; i++) {
    const e = `${seed}-h1-${i}`;
    hop1.push(e);
    nodes.push(node(e, 1, 40 - i * 2, [seed, e]));
  }
  const hop2: Array<[string, string]> = [];
  for (const p of hop1) {
    for (let j = 0; j < 12; j++) {
      const e = `${p}-h2-${j}`;
      hop2.push([p, e]);
      nodes.push(node(e, 2, 12 - j, [seed, p, e]));
    }
  }
  let i3 = 0;
  while (nodes.length < 200) {
    const [p1, p2] = hop2[i3 % hop2.length];
    const e = `${p2}-h3-${i3}`;
    i3++;
    nodes.push(node(e, 3, 3, [seed, p1, p2, e]));
  }
  return { seed, seed_community_id: null, nodes, truncated: true };
}

export const NAMESPACE = "bench";

const WHOAMI = {
  authenticated: true,
  subject: "e2e@example.com",
  org: "dev",
  namespaces: [NAMESPACE],
  roles: ["PLATFORM_ADMIN"],
  is_service_account: false,
};

// One admin row of kind memory: the landing rule picks it whether or not a pick was
// saved.
const LIST_MINE = {
  rows: [
    {
      namespace: NAMESPACE,
      role: "admin",
      status: "active",
      visibility: "private",
      kind: "memory",
      description: "",
      joined_at: "2026-01-01T00:00:00Z",
      is_new: false,
    },
  ],
};

const TAXONOMY = {
  tags: [],
  entities: [
    { value: "alpha", count: 900 },
    { value: "beta", count: 400 },
    { value: "gamma", count: 100 },
  ],
  memory_types: [],
  memory_count: 1000,
  communities: [],
  communities_computed_at: "",
};

/** A taxonomy whose communities were computed three hours before the test's clock — the
 *  copy is "N communities · computed 3h ago". */
export function taxonomyWithCommunities(now = Date.now()) {
  const at = new Date(now - 3 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ");
  return {
    ...TAXONOMY,
    communities: [
      { community_id: 1, label: "alpha", size: 120, top_entities: ["alpha", "beta"] },
      { community_id: 2, label: "gamma", size: 40, top_entities: ["gamma"] },
    ],
    communities_computed_at: at,
  };
}

/** `facts.related` for an expand. Under `<seed>-h1-0`: three new neighbours and one
 *  sibling the tree already holds ("+3 · 1 already shown"); under `<seed>-h1-1`: only the
 *  seed and siblings ("0 new within one hop · 3 already shown"); anything else: three
 *  new. */
function relatedFor(entity: string): {
  entity: string;
  related: Array<{ entity: string; co_occur: number; role: string }>;
} {
  const m = /^(.+)-h1-(\d+)$/.exec(entity);
  const seed = m ? m[1] : entity;
  if (m && m[2] === "0") {
    return {
      entity,
      related: [
        { entity: `${seed}-h1-1`, co_occur: 9, role: "both" },
        { entity: `${entity}-new-0`, co_occur: 8, role: "subject" },
        { entity: `${entity}-new-1`, co_occur: 5, role: "object" },
        { entity: `${entity}-new-2`, co_occur: 2, role: "both" },
      ],
    };
  }
  if (m && m[2] === "1") {
    return {
      entity,
      related: [
        { entity: seed, co_occur: 19, role: "both" },
        { entity: `${seed}-h1-0`, co_occur: 9, role: "both" },
        { entity: `${seed}-h1-2`, co_occur: 3, role: "both" },
      ],
    };
  }
  return { entity, related: [0, 1, 2].map((i) => ({ entity: `${entity}-new-${i}`, co_occur: 6 - i, role: "both" })) };
}

export interface RecordedCalls {
  traverse: Record<string, unknown>[];
  related: Record<string, unknown>[];
  search: Record<string, unknown>[];
  facts: Record<string, unknown>[];
  qa: Record<string, unknown>[];
  /** `namespace.join` / `cancel_request` / `leave` bodies, tagged with `op`. */
  join: Record<string, unknown>[];
  /** How many times `tracker.source.list` was read. */
  trackerSources: number;
  /** How many times `knowledge.source.list` was read. */
  knowledgeSources: number;
  /** Every `/v1/memory.*` path the page requested — a code/QA-only token must see none. */
  memory: string[];
  /** Every `memory.list` body — the page asks for the fields it renders and the preview
   *  length it shows. */
  list: Record<string, unknown>[];
  /** Every `knowledge.list` body — the Library page asks for the fields its rows read. */
  knowledge: Record<string, unknown>[];
  /** Every `audit.list` body — the Activity page asks for the four fields its rows read. */
  audit: Record<string, unknown>[];
  other: string[];
}

// ---- tracker sources: the `tracker.source.list` shape as the server writes it — one
// configured source with an open and a restricted namespace, and one declared source
// without a credential whose namespace the leader has not registered yet.

export interface TrackerSourceNamespaceFixture {
  namespace: string;
  kind: "qa";
  visibility: string;
  repos: string[];
  classification: "standard" | "restricted";
  join_policy: "open" | "approval" | "closed";
  registered: boolean;
  member_role: string;
  pending_role: string;
}
export interface TrackerSourceFixture {
  name: string;
  host: string;
  port: number;
  database: string;
  configured: boolean;
  /** A missing origin reads as "config". */
  origin?: "config" | "registry" | "alias";
  contract: {
    ok: boolean | null;
    summary: string;
    checked_at: string;
    optional_present: string[];
    features: Record<string, unknown>;
  };
  breaker: { state: string; consecutive_failures: number };
  freshness: Record<string, unknown>;
  namespaces: TrackerSourceNamespaceFixture[];
}

export const TRACKER_PUBLIC_NS = "tracker-clickhouse";
export const TRACKER_PRIVATE_NS = "tracker-example-private";

export function trackerSourcesFixture(): TrackerSourceFixture[] {
  return [
    {
      name: "qa",
      host: "qa.example",
      port: 9440,
      database: "qa_intelligence",
      configured: true,
      contract: {
        ok: true,
        summary: "every table and column present",
        checked_at: "2026-09-16T17:27:00+00:00",
        optional_present: ["github_checks"],
        features: {},
      },
      breaker: { state: "CLOSED", consecutive_failures: 0 },
      freshness: { issues: 22579, prs: 65328, last_refresh_at: "2026-09-16 17:24:59" },
      namespaces: [
        {
          namespace: TRACKER_PUBLIC_NS,
          kind: "qa",
          visibility: "private",
          repos: ["ClickHouse/ClickHouse"],
          classification: "standard",
          join_policy: "open",
          registered: true,
          member_role: "",
          pending_role: "",
        },
        {
          namespace: TRACKER_PRIVATE_NS,
          kind: "qa",
          visibility: "private",
          repos: ["ClickHouse/example-private"],
          classification: "restricted",
          join_policy: "approval",
          registered: true,
          member_role: "",
          pending_role: "",
        },
      ],
    },
    {
      name: "clickstack",
      host: "cs.example",
      port: 0,
      database: "loom_tracker",
      configured: false,
      origin: "registry",
      contract: { ok: null, summary: "unchecked", checked_at: "", optional_present: [], features: {} },
      breaker: { state: "unconfigured", consecutive_failures: 0 },
      freshness: {},
      namespaces: [
        {
          namespace: "tracker-clickstack",
          kind: "qa",
          visibility: "private",
          repos: ["ClickHouse/clickstack"],
          classification: "standard",
          join_policy: "open",
          registered: false,
          member_role: "",
          pending_role: "",
        },
      ],
    },
  ];
}

// ---- Repositories: `code.repos` rows for one repository with three branches, and the
// caller's standing on each.

export interface CodeRepoFixture {
  org: string;
  namespace: string;
  repo: string;
  branch: string;
  git_head: string;
  clone_head: string;
  fast_tier_lag: number;
  scip_lag: number;
  scip_py_lag: number;
  indexed_at: string;
  last_refresh_at: string;
  counts: Record<string, number>;
  join_policy: "open" | "approval" | "closed";
  my_status: "none" | "pending" | "member";
  my_role: "" | "viewer" | "writer" | "admin";
  source: "config" | "registry";
  searchable: boolean;
}

// `knowledge.formats` as a server answers it with the reference extractor (every format
// available): each known format sorted by name, with the extensions and media types that
// name it (`server/core/document_formats.py`).
const KNOWLEDGE_FORMATS = {
  formats: [
    {
      format: "csv",
      extensions: ["csv", "tsv"],
      media_types: ["text/csv", "text/tab-separated-values"],
      available: true,
    },
    {
      format: "docx",
      extensions: ["docx"],
      media_types: ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
      available: true,
    },
    {
      format: "html",
      extensions: ["htm", "html", "xhtml"],
      media_types: ["application/xhtml+xml", "text/html"],
      available: true,
    },
    { format: "markdown", extensions: ["markdown", "md", "mdx"], media_types: ["text/markdown"], available: true },
    { format: "parquet", extensions: ["parquet"], media_types: ["application/vnd.apache.parquet"], available: true },
    { format: "pdf", extensions: ["pdf"], media_types: ["application/pdf"], available: true },
    {
      format: "pptx",
      extensions: ["pptx"],
      media_types: ["application/vnd.openxmlformats-officedocument.presentationml.presentation"],
      available: true,
    },
    { format: "sql", extensions: ["sql"], media_types: ["application/sql"], available: true },
    { format: "text", extensions: ["log", "rst", "text", "txt"], media_types: ["text/plain"], available: true },
    { format: "toml", extensions: ["toml"], media_types: ["application/toml"], available: true },
    {
      format: "xlsx",
      extensions: ["xlsx"],
      media_types: ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
      available: true,
    },
  ],
  max_upload_bytes: 64 * 1024 * 1024,
  refused_extensions: ["doc", "ppt", "xls"],
  default_format: "text",
};

/** Roles `namespace.join` can grant; a join never yields a non-member, so `""` is not one. */
const JOIN_ROLES = ["viewer", "writer", "admin"] as const;

function isJoinRole(v: unknown): v is (typeof JOIN_ROLES)[number] {
  return JOIN_ROLES.some((r) => r === v);
}

export const CODE_REPO = "ClickHouse/ClickHouse";

export function codeReposFixture(now = Date.now()): CodeRepoFixture[] {
  const indexed = new Date(now - 2 * 3600 * 1000).toISOString().slice(0, 19).replace("T", " ");
  const row = (
    namespace: string,
    branch: string,
    join_policy: CodeRepoFixture["join_policy"] = "open",
  ): CodeRepoFixture => ({
    org: "dev",
    namespace,
    repo: CODE_REPO,
    branch,
    git_head: "780b519ea1234567",
    clone_head: "780b519ea1234567",
    fast_tier_lag: 0,
    scip_lag: 0,
    scip_py_lag: 0,
    indexed_at: indexed,
    last_refresh_at: indexed,
    counts: { code_files: 31539, code_symbols: 412000 },
    join_policy,
    my_status: "none",
    my_role: "",
    source: "config",
    searchable: true,
  });
  return [
    row("code-clickhouse", "master"),
    row("code-clickhouse-25-8", "25.8"),
    row("code-clickhouse-25-7", "25.7", "approval"),
  ];
}

/** One document source on the trunk's namespace, as `knowledge.source.list` writes it:
 *  its last turn synced and left one file unconverted. */
export function knowledgeSourcesFixture(): Record<string, unknown>[] {
  return [
    {
      name: "design-docs",
      kind: "gdrive",
      config: { folder_ids: ["1AbCdEfGhIjKlMnOp"], include: ["docs", "slides", "pdf", "pptx"] },
      created_by: "maintainer@example.com",
      created_at: "2026-10-01T00:00:00Z",
      sync: {
        requested_at: "",
        last_synced_at: "2026-10-02T00:00:00Z",
        status: "ok",
        documents_seen: 12,
        changed: 3,
        removed: 0,
        unconverted: 1,
        unconverted_reasons: { too_large: 1 },
        skipped: 0,
        error: "",
      },
    },
  ];
}

/** A token with the flat VIEWER role and no admin claim. */
const WHOAMI_VIEWER = { ...WHOAMI, roles: ["VIEWER"] };

// ---- Patterns mode: the tracker plane's answers for the bound namespace — the binding,
// the board and gaps, a cell's area plan, the record of 97288 (caused by PR 90220, fixed
// by four PRs), duplicates.

const TRACKER_BINDING_BOUND = {
  tracker_namespace: "tracker-bench",
  restricted: false,
  kind: "code",
  authorized: true,
  templates: [
    "similar_bugs",
    "area_open_issues",
    "component_risk",
    "release_blockers",
    "changelog",
    "prs_touching",
    "person_activity",
    "test_gaps",
    "flaky",
    "item",
    "duplicates",
    "severity_board",
  ],
};
const TRACKER_BINDING_NONE = {
  tracker_namespace: "",
  restricted: false,
  kind: "none",
  authorized: false,
  templates: [],
};
// A restricted tracker namespace: the restricted class is served there.
const TRACKER_BINDING_RESTRICTED = {
  tracker_namespace: "tracker-bench-private",
  restricted: true,
  kind: "code",
  authorized: true,
  templates: [
    ...TRACKER_BINDING_BOUND.templates,
    "escalation_context",
    "incident_context",
    "customer_state",
    "area_incidents",
    "area_escalations",
    "customers_in_area",
    "slack_threads",
    "version_exposure",
    "version_limits",
  ],
};

const BOARD_ROWS = [
  { component: "MergeTree", severity: "medium", open: 27, untested: 20, flaky: 1, bisected: 8 },
  { component: "MergeTree", severity: "low", open: 206, untested: 178, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "critical", open: 2, untested: 2, flaky: 0, bisected: 0 },
  { component: "Keeper", severity: "low", open: 40, untested: 30, flaky: 0, bisected: 3 },
  { component: "Interpreter", severity: "medium", open: 5, untested: 5, flaky: 0, bisected: 0 },
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
const ISSUE = (number: number, title: string, severity_level: string) => ({
  repo: "ClickHouse/ClickHouse",
  number,
  is_pr: 0,
  state: "open",
  title,
  labels: ["bug", "comp-mergetree"],
  primary_component: "MergeTree",
  severity_level,
  is_flaky_test: 0,
  bisect_outcome: number === 97288 ? "reproduced" : "",
  has_test: -1,
  url: `https://github.com/ClickHouse/ClickHouse/issues/${number}`,
  opened_at: "2026-09-01T00:00:00",
  closed_at: null,
});
const AREA_ROWS = [
  ISSUE(97288, "Regression in parameterized views (After parameters substitution)", "medium"),
  ISSUE(119601, "Zero-length columns_substreams.txt after power loss detaches the whole Wide part", "medium"),
  ISSUE(119000, "A low one", "low"),
];
// The restricted class of MergeTree's area plan: one core incident, no escalation in the
// window.
const INCIDENT_ROWS = [
  {
    source: "core_incident",
    source_id: "acme/incidents#9",
    url: "https://example.invalid/incidents/9",
    title: "Logical error in MergeTreeData::loadDataParts",
    severity: "critical",
    component: "MergeTree",
    created_at: "2026-09-01T00:00:00",
    closed_at: null,
    open: true,
    has_test: false,
    error_type: "LOGICAL_ERROR",
    error_name: "",
    signal: "",
    top_function: "src/Storages/MergeTree/MergeTreeData.cpp",
  },
];
// The `duplicates` template's rows for 119601: 116490 and 116481 are `related_to`
// (direction "out": the seed relates to them), not `duplicate_of` — the picture must say
// so.
const DUPLICATE_ROWS: Record<number, Record<string, unknown>[]> = {
  119601: [
    {
      source: "github_issue",
      source_id: "ClickHouse/ClickHouse#116490",
      url: "https://github.com/ClickHouse/ClickHouse/issues/116490",
      title: "Wide part detached after power loss (columns_substreams.txt empty)",
      severity: "medium",
      component: "MergeTree",
      created_at: "2026-07-01T00:00:00",
      closed_at: null,
      open: true,
      has_test: null,
      rel_type: "related_to",
      direction: "out",
      state_reason: "",
    },
    {
      source: "github_issue",
      source_id: "ClickHouse/ClickHouse#116481",
      url: "https://github.com/ClickHouse/ClickHouse/issues/116481",
      title: "Detached parts after unclean shutdown",
      severity: "low",
      component: "MergeTree",
      created_at: "2026-06-30T00:00:00",
      closed_at: null,
      open: true,
      has_test: null,
      rel_type: "related_to",
      direction: "out",
      state_reason: "",
    },
  ],
};
const ITEMS: Record<number, Record<string, unknown>> = {
  97288: {
    number: 97288,
    kind: "issue",
    state: "open",
    is_merged: false,
    title: "Regression in parameterized views (After parameters substitution)",
    author: "a",
    opened_at: "2026-09-01",
    closed_at: "",
    url: "https://github.com/ClickHouse/ClickHouse/issues/97288",
    tracker: {
      kind: "issue",
      severity: "medium",
      labels: ["bug"],
      fixed_by: [97289, 97304, 97306, 98708],
      caused_by: [90220],
      url: "https://github.com/ClickHouse/ClickHouse/issues/97288",
    },
    line: "[qa] issue #97288 · medium · fixed by #97289 #97304 #97306 #98708 · caused by #90220",
  },
  90220: {
    number: 90220,
    kind: "pr",
    state: "closed",
    is_merged: true,
    title: "Show schema for parameterized_view",
    author: "b",
    opened_at: "2026-08-01",
    closed_at: "2026-08-02",
    url: "https://github.com/ClickHouse/ClickHouse/pull/90220",
    tracker: { kind: "pr", component: "Interpreter", fixes: [88875] },
    line: "[qa] pr #90220",
  },
  97289: {
    number: 97289,
    kind: "pr",
    state: "closed",
    is_merged: true,
    title: "Fix parameterized view substitution",
    author: "c",
    opened_at: "",
    closed_at: "",
    url: "https://github.com/ClickHouse/ClickHouse/pull/97289",
    tracker: { kind: "pr" },
    line: "",
  },
  119601: {
    number: 119601,
    kind: "issue",
    state: "open",
    is_merged: false,
    title: "Zero-length columns_substreams.txt after power loss detaches the whole Wide part",
    author: "d",
    opened_at: "2026-09-01",
    closed_at: "",
    url: "https://github.com/ClickHouse/ClickHouse/issues/119601",
    tracker: {
      kind: "issue",
      severity: "medium",
      component: "MergeTree",
      labels: ["bug"],
      url: "https://github.com/ClickHouse/ClickHouse/issues/119601",
    },
    line: "[qa] issue #119601 · medium · MergeTree",
  },
  116490: {
    number: 116490,
    kind: "issue",
    state: "open",
    is_merged: false,
    title: "Wide part detached after power loss (columns_substreams.txt empty)",
    author: "e",
    opened_at: "2026-07-01",
    closed_at: "",
    url: "https://github.com/ClickHouse/ClickHouse/issues/116490",
    tracker: { kind: "issue", severity: "medium", component: "MergeTree" },
    line: "[qa] issue #116490 · medium · MergeTree",
  },
  116481: {
    number: 116481,
    kind: "issue",
    state: "open",
    is_merged: false,
    title: "Detached parts after unclean shutdown",
    author: "f",
    opened_at: "2026-06-30",
    closed_at: "",
    url: "https://github.com/ClickHouse/ClickHouse/issues/116481",
    tracker: { kind: "issue", severity: "low", component: "MergeTree" },
    line: "[qa] issue #116481 · low · MergeTree",
  },
};

function trackerAnswerFor(body: Record<string, unknown>, restricted = false): Record<string, unknown> {
  const q = String(body.question ?? "");
  const anchors = isRecord(body.anchors) ? body.anchors : {};
  const section = (template: string, status: string, rows: unknown[], params: Record<string, unknown> = {}) => ({
    template,
    title: template,
    access: "public",
    params,
    why: "e2e",
    status,
    cites: "",
    rows,
  });
  if (q === "bug board")
    return {
      intent: "severity_board",
      anchors,
      sections: [section("severity_board", "served", BOARD_ROWS)],
      searched: ["severity_board()"],
      lines: [],
      restricted,
      verdict: "answered",
    };
  if (q === "test gaps")
    return {
      intent: "test_gaps",
      anchors,
      sections: [section("test_gaps", "served", GAP_ROWS)],
      searched: [],
      lines: [],
      restricted,
      verdict: "answered",
    };
  if (q === "duplicates") {
    const n = Number((Array.isArray(anchors.numbers) ? anchors.numbers : [])[0]);
    const rows = DUPLICATE_ROWS[n] ?? [];
    return {
      intent: "duplicates",
      anchors,
      sections: [section("duplicates", rows.length ? "served" : "empty", rows, { number: n, limit: 20 })],
      searched: [],
      lines: [],
      restricted,
      verdict: rows.length ? "answered" : "empty",
    };
  }
  if (Array.isArray(anchors.components)) {
    const c = String(anchors.components[0]);
    // The cell's severities filter the read on the server and lift its ceiling to 300;
    // without them the read is the top 50.
    const sevs = Array.isArray(anchors.severities) ? anchors.severities.map(String) : [];
    const open = c === "MergeTree" ? AREA_ROWS.filter((r) => !sevs.length || sevs.includes(r.severity_level)) : [];
    const openParams = sevs.length ? { component: c, severities: sevs, limit: 300 } : { component: c, limit: 50 };
    const area = { component: c, days: 90 };
    return {
      intent: "area_risk",
      anchors,
      searched: [],
      lines: [],
      restricted,
      verdict: "answered",
      sections: [
        restricted
          ? section(
              "area_incidents",
              c === "MergeTree" ? "served" : "empty",
              c === "MergeTree" ? INCIDENT_ROWS : [],
              area,
            )
          : section("area_incidents", "restricted_required", [], area),
        restricted
          ? section("area_escalations", "empty", [], area)
          : section("area_escalations", "restricted_required", [], area),
        section("component_risk", "served", [{ component: c }], { components: [c] }),
        section("area_open_issues", open.length ? "served" : "empty", open, openParams),
      ],
    };
  }
  if (Array.isArray(anchors.quoted))
    return {
      intent: "similar_bugs",
      anchors,
      sections: [
        section("similar_bugs", "served", [
          { source_id: "ClickHouse/ClickHouse#1", title: "sibling", severity: "low", url: "", open: true },
        ]),
      ],
      searched: [],
      lines: [],
      restricted: false,
      verdict: "answered",
    };
  return { intent: "", anchors, sections: [], searched: [], lines: [], restricted: false, verdict: "unknown" };
}

function trackerItemsFor(body: Record<string, unknown>): Record<string, unknown> {
  const nums = (Array.isArray(body.numbers) ? body.numbers : []).map(Number);
  return {
    items: nums.filter((n) => ITEMS[n]).map((n) => ITEMS[n]),
    unknown: nums.filter((n) => !ITEMS[n]),
    tracker_unavailable: false,
  };
}

export interface InstallOptions {
  /** Answer `tracker.binding` as a bound code namespace (default: none). */
  trackerBound?: boolean;
  /** Answer `namespace.taxonomy` with this instead of `TAXONOMY`. */
  taxonomy?: Record<string, unknown>;
  /** The bound tracker namespace is restricted: incidents and escalations are served. */
  trackerRestricted?: boolean;
  /** Answer `tracker.source.list` with these sources (joins flip their standing), or 404
   *  it ("absent": a server without the operation). Default: absent. */
  trackerSources?: "absent" | TrackerSourceFixture[];
  /** Answer `knowledge.source.list` with these sources and `enabled: true`, or 404 it
   *  ("absent"). Default: none, sync off. */
  knowledgeSources?: "absent" | Record<string, unknown>[];
  /** Sign in as a flat VIEWER instead of a platform admin. */
  viewer?: boolean;
  /** The token's `namespaces` claim (default: `[NAMESPACE]`). */
  claims?: string[];
  /** The `namespace.list_mine` rows (default: `LIST_MINE.rows`, plus any joined tracker
   *  source namespaces). */
  mine?: Record<string, unknown>[];
  /** The saved pick in localStorage; `null` saves none (default: `NAMESPACE`). */
  savedNamespace?: string | null;
  /** Answer `code.repos` with these rows (joins flip their standing). Default: none. */
  codeRepos?: CodeRepoFixture[];
  /** Answer `memory.list` with this many rows, each carrying a ~600-character preview — a
   *  render-budget fixture. Default: the empty list. */
  memoryRows?: number;
  /** The first memory row's tags, in place of the fixture's three. */
  firstMemoryTags?: string[];
  /** The first memory row's agent, in place of the fixture's. */
  firstMemoryAgent?: string;
  /** Answer `knowledge.list` with this many rows. */
  knowledgeRows?: number;
  /** Answer `run.list` with this many runs, every field a run carries, projected by the
   *  request's `fields`. Default: none. */
  runRows?: number;
  /** Answer `conversation.threads` with this many threads, cut to the request's `limit`,
   *  and `conversation.search` with three turns of each. Default: none. */
  threadRows?: number;
  /** Answer `audit.list` with this many events. Default: none. */
  auditRows?: number;
  /** Answer `namespace.browse` with this many memory namespaces. Default: none. */
  browseRows?: number;
  /** Answer `task.list` with this many open tasks, spread over the four open lanes, in
   *  place of the three the board shows by default. */
  taskRows?: number;
  /** Hold every `memory.search` answer this long, so a retype can be seen to abort the
   *  stale search. Default: answer at once. */
  searchDelayMs?: number;
  /** Hold every `facts.related` answer this long, so a re-walk can be seen to drop the
   *  expand it started over. Default: answer at once. */
  relatedDelayMs?: number;
  /** Hold every `facts.traverse` answer this long, so an expand can be started while a
   *  re-walk is still loading. Default: answer at once. */
  traverseDelayMs?: number;
  /** Answer the Profile page's `memory.list` (`memory_type: "profile"`) with this many
   *  sections, each short enough to fit the list's preview (`value_truncated: false`), so
   *  the page has no row to read after the list. Default: the empty list. */
  profileRows?: number;
}

/** A token that names only a code index and a tracker namespace — a QA team's or a
 *  code-index service account's persona. `list_mine` stamps `kind` on the claim-only rows
 *  and on grant rows alike (`server/identity/app/namespace.py`); the QA row here is a grant.
 *  */
export const CODE_NS = "code-clickhouse";
export const CODE_TRACKER_ONLY_CLAIMS = [CODE_NS, TRACKER_PUBLIC_NS];
export const CODE_TRACKER_ONLY_MINE = [
  {
    namespace: CODE_NS,
    role: "jwt",
    status: "active",
    visibility: "private",
    kind: "code",
    description: "ClickHouse/ClickHouse @ master",
    joined_at: null,
    is_new: false,
  },
  {
    namespace: TRACKER_PUBLIC_NS,
    role: "viewer",
    status: "active",
    visibility: "private",
    kind: "qa",
    description: "",
    joined_at: "2026-09-16T00:00:00Z",
    is_new: false,
  },
];

/** Sign the page in with a stored token and answer every API call locally. */
export async function installApi(page: Page, options: InstallOptions = {}): Promise<RecordedCalls> {
  const calls: RecordedCalls = {
    traverse: [],
    related: [],
    search: [],
    facts: [],
    qa: [],
    join: [],
    trackerSources: 0,
    knowledgeSources: 0,
    memory: [],
    list: [],
    knowledge: [],
    audit: [],
    other: [],
  };
  // The tracker sources and the caller's standing on each namespace; a join, a withdrawal
  // or a leave flips the standing the next list returns, and list_mine grows a `kind: qa`
  // grant row for a joined namespace.
  const trackerSources =
    options.trackerSources === "absent" || options.trackerSources === undefined ? null : options.trackerSources;
  const trackerNamespace = (name: string) =>
    trackerSources?.flatMap((s) => s.namespaces).find((n) => n.namespace === name);
  const codeRepos = options.codeRepos ?? [];
  const codeRow = (name: string) => codeRepos.find((r) => r.namespace === name);
  const mineRows = () => ({
    rows: [
      ...(options.mine ?? LIST_MINE.rows),
      ...(trackerSources ?? [])
        .flatMap((s) => s.namespaces)
        .filter((n) => n.member_role || n.pending_role)
        .map((n) => ({
          namespace: n.namespace,
          role: n.member_role || "viewer",
          status: n.member_role ? "active" : "pending",
          pending_role: n.pending_role || null,
          visibility: n.visibility,
          kind: "qa",
          description: "",
          joined_at: "2026-09-16T00:00:00Z",
          is_new: false,
        })),
    ],
  });
  const saved = options.savedNamespace === undefined ? NAMESPACE : options.savedNamespace;
  await page.addInitScript((ns) => {
    localStorage.setItem("loom_token_v1", "e2e-token");
    if (ns !== null) localStorage.setItem("loom_namespace_v1", ns);
  }, saved);
  const who = {
    ...(options.viewer ? WHOAMI_VIEWER : WHOAMI),
    ...(options.claims ? { namespaces: options.claims } : {}),
  };
  await page.route("**/v1/**", async (route) => {
    const url = new URL(route.request().url());
    let body: Record<string, unknown> = {};
    try {
      const raw: unknown = route.request().postDataJSON();
      body = isRecord(raw) ? raw : {};
    } catch {
      /* not JSON */
    }
    const json = (b: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    const ns = String(body.namespace ?? "");
    if (url.pathname.startsWith("/v1/memory.")) calls.memory.push(url.pathname);
    switch (url.pathname) {
      case "/v1/whoami":
        return json(who);
      // Well-shaped empties for the two summaries the Sharing and Usage views read fields
      // of; the default answer's bare rows/entries would make them throw.
      case "/v1/me/sharing-summary":
        return json({
          subject: "",
          org: "",
          window_days: 0,
          totals: { writes: 0, recalls: 0, cross_namespace_recalls: 0, namespaces: 0, agents: 0 },
          writes: 0,
          recalls: 0,
          cross_namespace_recalls: 0,
          namespaces: [],
          agents: [],
          agent_namespaces: [],
        });
      // Two collections, one inside the other, so the Library's select draws a tree.
      case "/v1/collection.list":
        return json({
          collections: [
            { namespace: "col-handbook", parent: "", personal: false, role: "admin", description: "Team handbook" },
            {
              namespace: "col-handbook-oncall",
              parent: "col-handbook",
              personal: false,
              role: "writer",
              description: "",
            },
          ],
          next_cursor: null,
        });
      case "/v1/admin.users":
        return json({ users: [], platform_admins_configured: [] });
      case "/v1/cost.budget_status":
        return json({
          namespace: "team-alerts",
          spent_24h_usd: 0,
          cap_usd: 0,
          cap_source: "uncapped",
          over_cap: false,
          pct_of_cap: null,
          namespaces_with_spend: 0,
        });
      case "/v1/audit.list":
        calls.audit.push(body);
        return json({ rows: auditRowsFixture(options.auditRows ?? 0), next_cursor: null });
      case "/v1/namespace.browse":
        if (!options.browseRows) break;
        return json({ rows: browseRowsFixture(options.browseRows) });
      case "/v1/ui.vitals":
        return json({ recorded: true, route: body.route ?? "" });
      case "/v1/memory.list":
        calls.list.push(body);
        if (body.memory_type === "profile" && options.profileRows) {
          return json({
            entries: profileRowsFixture(options.profileRows, ns),
            total: options.profileRows,
            today: 0,
            truncated: false,
            namespace_version: "1",
            unchanged: false,
          });
        }
        if (!options.memoryRows) break;
        {
          // One page of the rows, as the server answers `limit`, `offset` and `preview_chars`:
          // a longer value is cut to `preview_chars`, ends in "…" and is flagged `value_truncated`.
          const from = Number(body.offset ?? 0);
          const cut = typeof body.preview_chars === "number" ? body.preview_chars : Infinity;
          const rows = memoryRowsFixture(options.memoryRows, ns, options.firstMemoryTags, options.firstMemoryAgent)
            .slice(from, from + Number(body.limit ?? 100))
            .map((r) =>
              typeof r.value === "string" && r.value.length > cut
                ? { ...r, value: `${r.value.slice(0, cut)}…`, value_truncated: true }
                : r,
            );
          return json({
            entries: projectRows(rows, body, "memory_key"),
            total: options.memoryRows,
            today: options.memoryRows,
            truncated: false,
            namespace_version: "1",
            unchanged: false,
          });
        }
      case "/v1/memory.get": {
        // The inspector's one read on open answers from the same fixture rows, so a card
        // open can be measured end to end.
        if (!options.memoryRows) break;
        const rows = memoryRowsFixture(options.memoryRows, ns, options.firstMemoryTags, options.firstMemoryAgent);
        const hit = rows.find((r) => r.memory_key === body.memory_key);
        if (!hit) return json({ found: false });
        return json({
          ...hit,
          found: true,
          recall_count: 0,
          last_recalled_at: null,
          updated_at: hit.created_at,
          status: "active",
        });
      }
      case "/v1/knowledge.formats":
        return json(KNOWLEDGE_FORMATS);
      case "/v1/knowledge.list":
        calls.knowledge.push(body);
        if (!options.knowledgeRows) break;
        return json({
          rows: projectRows(knowledgeRowsFixture(options.knowledgeRows), body, "source_uri"),
          total: options.knowledgeRows,
          next_cursor: null,
        });
      case "/v1/namespace.list_mine":
        return json(mineRows());
      // The task inbox plane — one inbox owned by the namespace, two tasks in it, and the
      // bell's counts.
      case "/v1/inbox.list":
        return json({ inboxes: taskInboxesFixture(ns) });
      case "/v1/task.list":
        return json({
          tasks: options.taskRows
            ? openTaskRowsFixture(String(body.inbox ?? ""), options.taskRows)
            : taskRowsFixture(String(body.inbox ?? "")),
          truncated: false,
        });
      case "/v1/task.counts":
        return json({ counts: taskCountsFixture(ns) });
      case "/v1/task.get":
        return json(taskGetFixture(String(body.task_id ?? ""), ns));
      case "/v1/task.history":
        return json(taskHistoryFixture(String(body.task_id ?? "")));
      case "/v1/code.repos":
        return json({ repos: projectRows(codeRepos, body, "namespace") });
      case "/v1/run.list":
        if (!options.runRows) break;
        return json(runListFixture(runRowsFixture(options.runRows, ns), body));
      case "/v1/conversation.threads":
        if (!options.threadRows) break;
        return json({
          threads: projectRows(
            threadRowsFixture(options.threadRows).slice(
              Number(body.offset ?? 0),
              Number(body.offset ?? 0) + Number(body.limit ?? 20),
            ),
            body,
            "thread_id",
          ),
        });
      case "/v1/conversation.search":
        if (!options.threadRows) break;
        return json({ hits: conversationHitsFixture(options.threadRows, Number(body.top_k ?? 50)) });
      case "/v1/run.get":
        return json(runGetFixture(runRowsFixture(options.runRows ?? 0, ns), String(body.run_id ?? "")));
      case "/v1/tracker.source.list":
        calls.trackerSources++;
        if (trackerSources === null) {
          return route.fulfill({
            status: 404,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Not Found" }),
          });
        }
        return json({ sources: trackerSources, self_service: true });
      case "/v1/knowledge.source.list":
        calls.knowledgeSources++;
        if (options.knowledgeSources === "absent") {
          return route.fulfill({
            status: 404,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Not Found" }),
          });
        }
        return json({ sources: options.knowledgeSources ?? [], enabled: options.knowledgeSources !== undefined });
      case "/v1/namespace.join": {
        calls.join.push({ op: "join", ...body });
        const code = codeRow(ns);
        if (code && code.join_policy === "open") {
          code.my_status = "member";
          code.my_role = isJoinRole(body.role) ? body.role : "viewer";
          return json({ namespace: ns, role: code.my_role, status: "active", joined_at: "2026-09-16T00:00:00Z" });
        }
        if (code) {
          code.my_status = "pending";
          return json({
            namespace: ns,
            role: "viewer",
            status: "pending",
            requested_role: body.role ?? "viewer",
            pending_admins: ["maintainer@example.com"],
            joined_at: "",
          });
        }
        const target = trackerNamespace(ns);
        if (target && target.join_policy === "open") {
          target.member_role = String(body.role ?? "viewer");
          return json({ namespace: ns, role: target.member_role, status: "active", joined_at: "2026-09-16T00:00:00Z" });
        }
        if (target) {
          target.pending_role = String(body.role ?? "viewer");
          return json({
            namespace: ns,
            role: "viewer",
            status: "pending",
            requested_role: target.pending_role,
            pending_admins: ["maintainer@example.com"],
            joined_at: "",
          });
        }
        return json({ namespace: ns, role: "viewer", status: "active", joined_at: "2026-09-16T00:00:00Z" });
      }
      case "/v1/namespace.cancel_request": {
        calls.join.push({ op: "cancel", ...body });
        const code = codeRow(ns);
        if (code) code.my_status = "none";
        const target = trackerNamespace(ns);
        if (target) target.pending_role = "";
        return json({ cancelled: true, namespace: ns, cleared_role: "viewer", cancelled_at: "2026-09-16T00:00:00Z" });
      }
      case "/v1/namespace.leave": {
        calls.join.push({ op: "leave", ...body });
        const code = codeRow(ns);
        if (code) {
          code.my_status = "none";
          code.my_role = "";
        }
        const target = trackerNamespace(ns);
        if (target) target.member_role = "";
        return json({ left: true, namespace: ns });
      }
      case "/v1/namespace.taxonomy":
        return json(options.taxonomy ?? TAXONOMY);
      case "/v1/facts.traverse":
        calls.traverse.push(body);
        if (options.traverseDelayMs) await new Promise((r) => setTimeout(r, options.traverseDelayMs));
        return json(walk(String(body.seed_entity ?? "")));
      case "/v1/facts.related":
        calls.related.push(body);
        if (options.relatedDelayMs) await new Promise((r) => setTimeout(r, options.relatedDelayMs));
        return json(relatedFor(String(body.entity ?? "")));
      case "/v1/memory.search":
        calls.search.push(body);
        if (options.searchDelayMs) await new Promise((r) => setTimeout(r, options.searchDelayMs));
        return json({ results: [], embedding_model: "e2e" });
      case "/v1/facts.list":
        calls.facts.push(body);
        return json({ rows: [] });
      case "/v1/tracker.binding": {
        calls.qa.push({ op: "binding", ...body });
        // A tracker namespace binds to itself (server/tracker/app/binding.py): the
        // Explorer opened from a tracker source card lands in Patterns mode there.
        const own = trackerNamespace(ns);
        if (own)
          return json({
            ...TRACKER_BINDING_BOUND,
            tracker_namespace: ns,
            kind: "qa",
            restricted: own.classification === "restricted",
          });
        return json(
          options.trackerRestricted
            ? TRACKER_BINDING_RESTRICTED
            : options.trackerBound
              ? TRACKER_BINDING_BOUND
              : TRACKER_BINDING_NONE,
        );
      }
      case "/v1/tracker.answer":
        calls.qa.push({ op: "answer", ...body });
        return json(trackerAnswerFor(body, Boolean(options.trackerRestricted)));
      case "/v1/tracker.item":
        calls.qa.push({ op: "item", ...body });
        return json(trackerItemsFor(body));
      default:
        break;
    }
    calls.other.push(url.pathname);
    return json({ rows: [], entries: [], total: 0, today: 0, truncated: false });
  });
  return calls;
}

/** A fixture answers what a projecting server would send. When the request carries
 *  `fields`, every row keeps only those keys (plus the operation's always-field) — so a
 *  page that reads a field its list forgot to ask for fails here, not in production (a
 *  missing field crashes the row; a key the server lacks is a 422). */
function projectRows<T extends object>(
  rows: T[],
  body: Record<string, unknown>,
  always: keyof T & string,
): Partial<T>[] {
  const fields = body.fields;
  if (!Array.isArray(fields) || fields.length === 0) return rows;
  const keep = new Set<unknown>([...fields, always]);
  return rows.map((r) => {
    const kept: Partial<T> = {};
    for (const k of keep) if (isKeyOf(r, k)) kept[k] = r[k];
    return kept;
  });
}

function isKeyOf<T extends object>(row: T, key: unknown): key is keyof T {
  return typeof key === "string" && key in row;
}

/** `n` run rows the way `run.list` returns them — every field a run carries, so a
 *  projected request shows what the page loses. */
function runRowsFixture(n: number, namespace: string): Record<string, unknown>[] {
  return Array.from({ length: n }, (_, i) => ({
    run_id: `run-${String(i).padStart(4, "0")}`,
    agent: i % 3 ? "alert-bot" : "oncall",
    user_id: "-",
    session_id: `s-${i % 7}`,
    goal: `Triage lane ${i % 5} alert burst`,
    user_request: "look at the lane",
    expected_outcome: "a triage note",
    environment: "prod",
    model: "claude-sonnet-5",
    consumer: "sre-agent",
    status: ["completed", "active", "failed"][i % 3],
    started_at: `2026-09-20T0${i % 10}:${String(i % 60).padStart(2, "0")}:00Z`,
    completed_at: i % 3 === 1 ? null : `2026-09-20T0${i % 10}:${String((i + 2) % 60).padStart(2, "0")}:00Z`,
    duration_ms: 1200 + i * 37,
    final_response: "done",
    confidence: 0.8,
    grade: "",
    grade_source: "",
    grade_reason: "",
    grade_ref: "",
    graded_at: "",
    memories_used: [`alerts/2026-09-19/${String(i).padStart(4, "0")}`],
    memories_written: i % 2 ? [`alerts/2026-09-20/${String(i).padStart(4, "0")}`] : [],
    metadata: { lane: i % 5 },
    budget_usd: 0,
    budget_seconds: 0,
    namespace,
    org: "acme",
  }));
}

/** `run.get` for one of `runs` (RunGetOutput in server/tasks/api/models_run.py): the
 *  row and what the server derives live — the keys recalled and written, the session's
 *  turns and cost, the budgets passed. An unknown id answers `found: false`. */
function runGetFixture(runs: Record<string, unknown>[], runId: string): Record<string, unknown> {
  const run = runs.find((r) => r.run_id === runId);
  if (!run) {
    return {
      found: false,
      run: null,
      recalled: -1,
      recalled_keys: [],
      memories_written: [],
      observations: 0,
      turns: 0,
      unpriced_turns: 0,
      cost_usd: 0,
      elapsed_seconds: 0,
      over_budget: [],
    };
  }
  return {
    found: true,
    run,
    recalled: 2,
    recalled_keys: [...(run.memories_used as string[]), "runbooks/ingest-lag"],
    memories_written: run.memories_written,
    observations: 1,
    turns: 4,
    unpriced_turns: 1,
    cost_usd: 0.042,
    elapsed_seconds: Math.round(Number(run.duration_ms) / 1000),
    over_budget: [],
  };
}

/** `n` memory rows the way `memory.list` returns them — a bounded string preview per row
 *  (the server caps `value` for a list), tags, an agent, alternating pins and a verified
 *  row here and there, so the card renders every chip it has. */
function memoryRowsFixture(
  n: number,
  namespace: string,
  firstTags?: string[],
  firstAgent?: string,
): Record<string, unknown>[] {
  const preview =
    "Alert fired on the ingest lane: consumer lag over 30 s for 4 minutes; root cause a stalled partition. ";
  return Array.from({ length: n }, (_, i) => ({
    memory_id: `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`,
    memory_key: `alerts/2026-09-19/${String(i).padStart(4, "0")}`,
    memory_type: i % 3 ? "semantic" : "episodic",
    confidence: 0.9,
    version: 1,
    created_at: `2026-09-19T0${i % 10}:${String(i % 60).padStart(2, "0")}:00Z`,
    expires_at: null,
    pinned: i % 17 === 0,
    tags: i === 0 && firstTags ? firstTags : ["alerts", i % 2 ? "ingest" : "storage", `lane-${i % 5}`],
    agent: i === 0 && firstAgent ? firstAgent : i % 4 ? "alert-bot" : "oncall",
    namespace,
    org: "acme",
    verification: i % 11 === 0 ? "human" : "none",
    verified_by: i % 11 === 0 ? "alice" : null,
    value: preview.repeat(6),
  }));
}

/** `n` profile sections the way `memory.list` returns them for `memory_type: "profile"` —
 *  a few hundred characters each, under the list's preview, with the value hash and cap
 *  the list carries. */
function profileRowsFixture(n: number, namespace: string): Record<string, unknown>[] {
  const labels = ["role", "team", "timezone", "preferences", "on-call", "languages"];
  return Array.from({ length: n }, (_, i) => ({
    memory_id: `00000000-0000-0000-0001-${String(i).padStart(12, "0")}`,
    memory_key: labels[i % labels.length] + (i >= labels.length ? `-${i}` : ""),
    memory_type: "profile",
    confidence: 1,
    version: 1 + i,
    created_at: "2026-09-19T08:00:00Z",
    updated_at: "2026-09-19T09:00:00Z",
    expires_at: null,
    pinned: false,
    tags: [],
    agent: "claude-desktop",
    namespace,
    org: "acme",
    verification: "none",
    verified_by: null,
    value_truncated: false,
    char_limit: null,
    content_sha256: "ab".repeat(32),
    value: `Section ${i}: prefers short answers, works UTC+1, on the ingest lane.`,
  }));
}

/** `n` Library rows the way `knowledge.list` returns them, every one with tags, a
 *  description and a blob. */
function knowledgeRowsFixture(n: number): Record<string, unknown>[] {
  return Array.from({ length: n }, (_, i) => ({
    source_uri: `docs/runbooks/handbook-${String(i).padStart(3, "0")}.md`,
    title: `Runbook ${i}: lane ${i % 5} recovery`,
    description:
      "Steps to drain, re-point and replay a stalled ingest lane without losing the at-least-once guarantee.",
    tags: ["ops", "runbook", `lane-${i % 5}`],
    ingested_at: "2026-09-19T00:00:00Z",
    chunk_count: 3 + (i % 4),
    superseded: false,
    has_blob: true,
    blob_size: 4096 + i,
    blob_content_type: "text/markdown",
    original_filename: `handbook-${i}.md`,
    snippet: "Drain the lane, re-point the consumer group, replay from the last committed offset.",
  }));
}

/** `n` audit events, newest first, each with an input behind its Details. */
function auditRowsFixture(n: number): Record<string, unknown>[] {
  return Array.from({ length: n }, (_, i) => ({
    ts: `2026-09-19T${String(23 - (i % 24)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00Z`,
    event_type: ["memory.set", "memory.delete", "memory.pin"][i % 3],
    actor: i % 2 ? "alice@example.com" : "alert-bot",
    subject: JSON.stringify({
      namespace: "team-alerts",
      memory_key: `alerts/2026-09-19/${String(i).padStart(4, "0")}`,
    }),
    request_id: `req-${i}`,
  }));
}

/** `n` memory namespaces the way `namespace.browse` lists them; none is the caller's. */
function browseRowsFixture(n: number): Record<string, unknown>[] {
  return Array.from({ length: n }, (_, i) => ({
    namespace: `team-${String(i).padStart(3, "0")}`,
    kind: "memory",
    description: `Lane ${i % 5} on-call notes and runbooks`,
    owner_email: "owner@example.com",
    member_count: 3 + (i % 7),
    requires_approval: i % 2 === 0,
    created_at: "2026-09-01T00:00:00Z",
    join_policy: i % 2 === 0 ? "approval" : "open",
    visibility: "public",
    my_status: "none",
    // The last row has a guide whose summary is longer than a row's name column.
    ...(i === n - 1
      ? {
          guide_revision: 4,
          guide_summary: "One runbook per service under runbook/<service>; tag stale marks a superseded one",
        }
      : {}),
  }));
}

/** `n` threads the way `conversation.threads` lists them, most recently active first. */
function threadRowsFixture(n: number): Record<string, unknown>[] {
  return Array.from({ length: n }, (_, i) => ({
    thread_id: `thread-${String(i).padStart(4, "0")}`,
    agent: i % 2 ? "claude-code" : "claude-desktop",
    msg_count: 4 + (i % 9),
    title: `Lane ${i % 5} exporter backlog`,
    first_seen: "2026-09-18T08:00:00Z",
    last_seen: `2026-09-19T${String(23 - (i % 24)).padStart(2, "0")}:00:00Z`,
    last_role: i % 2 ? "assistant" : "user",
    last_content_excerpt: "Drained the lane and replayed from the last committed offset; the backlog is clear.",
  }));
}

/** `conversation.search` over `threadRowsFixture(n)`: three turns of each thread, cut to
 *  `topK`. */
function conversationHitsFixture(n: number, topK: number): Record<string, unknown>[] {
  return threadRowsFixture(n)
    .flatMap((t, i) =>
      [0, 1, 2].map((k) => ({
        thread_id: t.thread_id,
        msg_id: `msg-${i}-${k}`,
        role: k % 2 ? "assistant" : "user",
        content_excerpt: `The lane ${i % 5} exporter backlog grew again; replay ${k} drained it.`,
        ts: t.last_seen,
      })),
    )
    .slice(0, topK);
}

// ---- Tasks: the inbox plane as the board reads it.

export const TASK_INBOX = "bench-bot";

function taskInboxesFixture(namespace: string): Record<string, unknown>[] {
  return [
    {
      org: "dev",
      inbox: TASK_INBOX,
      owner_agent: "bench-bot",
      owner_namespace: namespace,
      description: "the bench agent's queue",
      posters: ["sa:reporter-public-1a2b3c"],
      accept_disclosure: ["confidential", "internal", "public_ok"],
      default_lease_seconds: 1800,
      max_attempts: 3,
      max_defers: 5,
      triage_escalate_after_seconds: 14400,
      webhook_url: "",
      created_by: "human:e2e@example.com",
      created_at: "2026-09-20T08:00:00",
      updated_at: "2026-09-20T08:00:00",
      version: 1,
    },
  ];
}

function taskRowsFixture(inbox: string): Record<string, unknown>[] {
  if (inbox !== TASK_INBOX) return [];
  const row = (i: number, status: string, extra: Record<string, unknown> = {}) => ({
    task_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    inbox,
    kind: "bug_report",
    priority: 2 + (i % 2),
    status,
    reason: "",
    blocked_on: "",
    resume_at: null,
    deadline: null,
    not_before: null,
    defer_count: 0,
    title: `Task ${i}: a regression the public bot found`,
    from_agent: "reporter-public",
    from_subject: "sa:reporter-public-1a2b3c",
    owner: "",
    lease_until: null,
    parent_task_id: null,
    disclosure: "internal",
    created_at: `2026-09-20T0${i}:00:00`,
    updated_at: `2026-09-20T0${i}:00:00`,
    version: 1,
    ...extra,
  });
  return [
    row(1, "triage"),
    row(2, "todo"),
    row(3, "blocked", { reason: "needs a repro from the sender", blocked_on: "sender" }),
  ];
}

/** `n` open tasks dealt round-robin over triage, todo, in progress and blocked; the
 *  in-progress ones are leased to an agent. */
function openTaskRowsFixture(inbox: string, n: number): Record<string, unknown>[] {
  if (inbox !== TASK_INBOX) return [];
  const statuses = ["triage", "todo", "in_progress", "blocked"];
  return Array.from({ length: n }, (_, i) => ({
    task_id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
    inbox,
    kind: i % 2 ? "bug_report" : "question",
    priority: 1 + (i % 3),
    status: statuses[i % 4],
    reason: "",
    blocked_on: i % 4 === 3 ? "human" : "",
    resume_at: null,
    deadline: null,
    not_before: null,
    defer_count: 0,
    title: `Task ${i}: lane ${i % 5} exporter stalls`,
    from_agent: "reporter-public",
    from_subject: "sa:reporter-public-1a2b3c",
    owner: i % 4 === 2 ? "alert-bot" : "",
    lease_until: i % 4 === 2 ? "2099-01-01T00:00:00" : null,
    parent_task_id: null,
    disclosure: "internal",
    created_at: `2026-09-20T01:${String(i % 60).padStart(2, "0")}:00`,
    updated_at: "2026-09-20T01:00:00",
    version: 1,
  }));
}

/** `task.get` for one of `taskRowsFixture`'s tasks: the row with the fields only the
 *  opened task carries, its brief among them; `found: false` for any other id. */
function taskGetFixture(taskId: string, namespace: string): Record<string, unknown> {
  const row = taskRowsFixture(TASK_INBOX).find((t) => t.task_id === taskId);
  if (!row) return { found: false, task: null };
  const task = {
    ...row,
    org: "dev",
    namespace,
    origin: "bench-bot",
    attempt: 0,
    fence: 0,
    outcome: {},
    last_actor: "process:reporter-public",
    brief: {
      objective: "Find why the nightly bench run reports recall 0.62 where last week read 0.71.",
      acceptance_criteria: ["The cause is named, with the commit that brought it", "A test pins the fix"],
      inputs: { issue: "https://github.com/example/loom/issues/4121", bench: "recall-nightly" },
      constraints: "Do not re-run the full bench on the shared host.",
    },
  };
  return { found: true, task };
}

/** `task.history` for an opened task: it was posted, then a person edited it. */
function taskHistoryFixture(taskId: string): Record<string, unknown> {
  const event = (seq: number, name: string, actor: string, ts: string) => ({
    seq,
    task_id: taskId,
    inbox: TASK_INBOX,
    event: name,
    from_status: "",
    to_status: "",
    actor,
    agent: actor.startsWith("process:") ? actor.slice("process:".length) : "",
    fence: 0,
    version: seq,
    reason: "",
    patch: {},
    note: "",
    ts,
  });
  return {
    found: true,
    events: [
      event(1, "created", "process:reporter-public", "2026-09-20T01:00:00"),
      event(2, "updated", "human:alice@example.com", "2026-09-20T01:30:00"),
    ],
  };
}

function taskCountsFixture(namespace: string): Record<string, unknown>[] {
  void namespace;
  return [
    { inbox: TASK_INBOX, status: "triage", blocked_on: "", n: 1, oldest: "2026-09-20T01:00:00" },
    { inbox: TASK_INBOX, status: "todo", blocked_on: "", n: 1, oldest: "2026-09-20T02:00:00" },
    { inbox: TASK_INBOX, status: "blocked", blocked_on: "sender", n: 1, oldest: "2026-09-20T03:00:00" },
  ];
}

/** Every dashboard route, for the specs that sweep them all. */
export const ROUTES = [
  "/memories",
  "/recall",
  "/settings",
  "/repositories",
  "/flow",
  "/library",
  "/conversations",
  "/graph",
  "/assistant",
  "/spaces",
  "/profile",
  "/runs",
  "/tasks",
  "/admin/review_queue",
  "/admin/health",
  "/admin/guardrails",
  "/admin/policies",
  "/admin/activity",
  "/admin/usage",
  "/admin/agents",
  "/admin/users",
];

/** `run.list` as the server answers it: the filters (status, agent, memory_activity, q in
 *  the goal, run id or agent, any case), then `order_by`, then the page `offset` and
 *  `limit` cut, with `total` counting every run the filters keep. */
function runListFixture(all: Record<string, unknown>[], body: Record<string, unknown>): Record<string, unknown> {
  const q = String(body.q ?? "").toLowerCase();
  const used = (r: Record<string, unknown>) => (r.memories_used as string[]).length;
  const active = (r: Record<string, unknown>) => used(r) + (r.memories_written as string[]).length > 0;
  const kept = all.filter(
    (r) =>
      (!body.status || r.status === body.status) &&
      (!body.agent || r.agent === body.agent) &&
      (body.memory_activity !== "with" || active(r)) &&
      (body.memory_activity !== "without" || !active(r)) &&
      (!q || `${r.goal} ${r.run_id} ${r.agent}`.toLowerCase().includes(q)),
  );
  const order = String(body.order_by || "started_desc");
  const value = (r: Record<string, unknown>) =>
    order.startsWith("duration") ? Number(r.duration_ms) : order.startsWith("relied_on") ? used(r) : 0;
  const sign = order.endsWith("_asc") ? 1 : -1;
  kept.sort(
    (a, b) =>
      sign * (value(a) - value(b)) ||
      sign * String(a.started_at).localeCompare(String(b.started_at)) ||
      sign * String(a.run_id).localeCompare(String(b.run_id)),
  );
  const offset = Number(body.offset ?? 0);
  const page = kept.slice(offset, offset + Number(body.limit ?? 50));
  return { runs: projectRows(page, body, "run_id"), total: kept.length };
}

/** One unmeasured boot of /memories before a file's budgets run. The first page a fresh
 *  runner's browser loads also pays for reading Chromium, the fonts and the preview server's
 *  files from a cold disk; that cost is the runner's, not the app's, and it lands on whichever
 *  budget runs first in a CI part. */
export async function warmUp(browser: Browser, testInfo: TestInfo): Promise<void> {
  const page = await browser.newPage({ baseURL: testInfo.project.use.baseURL });
  try {
    await installApi(page);
    await page.goto("/memories", { waitUntil: "load" });
    await page.locator("main h1, main h2").first().waitFor();
  } finally {
    await page.close();
  }
}
