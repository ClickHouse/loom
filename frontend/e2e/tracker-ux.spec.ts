import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { CODE_REPO, codeReposFixture, installApi, isRecord, trackerSourcesFixture } from "./fixtures";

// The tracker and repository-docs flows in each state, at desktop and 400 px —
// screenshots for review (SHOT_TAG names the set), and the checks a person would make by
// looking: no sideways scroll at 400 px, every step reachable, the link line says what
// the link means.

const TAG = process.env.SHOT_TAG ?? "shot";
const SHOTS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../e2e-screenshots/tracker-ux");

async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${TAG}-${name}.png`), fullPage: true });
}

function maintainerRepos(bound: string | string[] = "tracker-clickhouse") {
  const list = Array.isArray(bound) ? bound : bound ? [bound] : [];
  return codeReposFixture().map((r) => ({
    ...r,
    my_status: "member" as const,
    my_role: "admin" as const,
    docs_namespace: "code-clickhouse",
    bound_trackers: list,
  }));
}

function sources() {
  const s = trackerSourcesFixture();
  // a registry tracker the caller administers, listing another repository
  s.push({
    name: "kj",
    host: "abc.clickhouse.cloud",
    port: 8443,
    database: "loom_tracker",
    configured: true,
    origin: "registry",
    contract: {
      ok: false,
      summary: "missing tables edges",
      checked_at: "2026-09-24T12:00:00+00:00",
      optional_present: [],
      features: {},
    },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: {},
    namespaces: [
      {
        namespace: "tracker-kj",
        kind: "qa",
        visibility: "private",
        repos: ["Acme/kj"],
        classification: "standard",
        join_policy: "open",
        registered: true,
        member_role: "admin",
        pending_role: "",
      },
    ],
  });
  return s;
}

const CONTRACT = {
  database: "loom_tracker",
  tables: [
    {
      table: "github_issues",
      required: true,
      note: "one row per issue",
      columns: [
        { name: "id", family: "UUID", type: "UUID", must_map: true, note: "a stable UUID per item" },
        { name: "source_id", family: "String", type: "String", must_map: true, note: "'Org/repo#number'" },
        { name: "labels", family: "Array(String)", type: "Array(String)", must_map: false, note: "the item's labels" },
      ],
    },
    {
      table: "github_prs",
      required: true,
      note: "one row per pull request",
      columns: [{ name: "pr_number", family: "UInt", type: "UInt64", must_map: true, note: "the PR number" }],
    },
    {
      table: "edges",
      required: true,
      note: "the links between items",
      columns: [{ name: "rel_type", family: "String", type: "String", must_map: true, note: "fixes / duplicate_of …" }],
    },
    {
      table: "core_incidents",
      required: false,
      note: "production incidents",
      columns: [{ name: "title", family: "String", type: "String", must_map: true, note: "" }],
    },
  ],
  templates: {
    github_issues:
      "CREATE OR REPLACE VIEW loom_tracker.github_issues AS\nSELECT\n    toUUID('0') AS id,  -- MUST map\n    '' AS source_id  -- MUST map\nFROM <your_table>;",
    github_prs:
      "CREATE OR REPLACE VIEW loom_tracker.github_prs AS\nSELECT\n    toUInt64(0) AS pr_number  -- MUST map\nFROM <your_table>;",
    edges: "CREATE OR REPLACE VIEW loom_tracker.edges AS\nSELECT\n    '' AS rel_type  -- MUST map\nFROM <your_table>;",
    core_incidents:
      "CREATE OR REPLACE VIEW loom_tracker.core_incidents AS\nSELECT\n    '' AS title  -- MUST map\nFROM <your_table>;",
  },
};

const failing = (dry = true) => ({
  name: "acme",
  host: "abc.clickhouse.cloud",
  database: "loom_tracker",
  namespaces: [{ namespace: "tracker-acme", repos: [CODE_REPO], classification: "standard", join_policy: "open" }],
  contract: {
    ok: false,
    database: "loom_tracker",
    summary: "missing tables edges",
    missing_tables: ["edges"],
    missing_columns: { github_prs: ["pr_number"] },
    type_mismatches: { github_prs: { additions: ["UInt", "String"] } },
    aliased: {},
    defaulted: {},
    optional_present: [],
    optional_incomplete: {},
  },
  roles: ["analyst"],
  my_role: "admin",
  dry_run: dry,
  written: !dry,
  view_templates: {
    github_prs:
      "CREATE OR REPLACE VIEW loom_tracker.github_prs AS\nSELECT\n    id,\n    toUInt64(0) AS pr_number,  -- MUST map: a default makes the row wrong\n    CAST(additions AS UInt64) AS additions  -- is String, the contract reads UInt\nFROM <your_table>;",
    edges:
      "CREATE OR REPLACE VIEW loom_tracker.edges AS\nSELECT\n    toUUID('00000000-0000-0000-0000-000000000000') AS from_id,  -- MUST map\n    '' AS rel_type  -- MUST map\nFROM <your_table>;",
  },
});
const passing = (dry = true) => ({
  ...failing(dry),
  contract: {
    ok: true,
    database: "loom_tracker",
    summary: "ok",
    missing_tables: [],
    missing_columns: {},
    optional_present: ["core_incidents"],
  },
  view_templates: {},
});

// A help desk of its own shape, no views — loom proposes the mapping
const TICKETS = {
  table: "tickets",
  engine: "ReplacingMergeTree",
  rows: 42,
  columns: [
    { name: "ticket_key", type: "String" },
    { name: "summary", type: "String" },
    { name: "details", type: "String" },
    { name: "created", type: "DateTime" },
    { name: "resolved", type: "Nullable(DateTime)" },
    { name: "priority", type: "LowCardinality(String)" },
  ],
};
const PROPOSED = {
  version: 1,
  tables: {
    github_issues: {
      source: "helpdesk.tickets",
      final: true,
      columns: {
        source_id: { concat: [{ lit: "Acme/widgets#" }, { digits: "ticket_key" }] },
        number: { digits: "ticket_key" },
        title: "summary",
        body: "details",
        created_at: "created",
        closed_at: "resolved",
        severity_level: "priority",
      },
    },
  },
};
const inspected = (views: boolean) => ({
  host: "abc.clickhouse.cloud",
  database: views ? "loom_tracker" : "helpdesk",
  tables: views ? [] : [TICKETS],
  roles: [],
  proposal: views
    ? { mode: "views", roles: {}, fields: {}, mapping: null }
    : {
        mode: "mapping",
        roles: { github_issues: "tickets" },
        mapping: PROPOSED,
        fields: {
          github_issues: {
            title: { from: "summary", confidence: "high" },
            body: { from: "details", confidence: "high" },
            closed_at: { from: "resolved", confidence: "medium" },
          },
        },
      },
});
const mapped = (dry = true) => ({
  ...passing(dry),
  database: "helpdesk",
  contract: {
    ok: true,
    mode: "mapping",
    database: "helpdesk",
    summary: "ok; mapped github_issues",
    missing_tables: [],
    missing_columns: {},
    optional_present: [],
    capabilities: {
      items: true,
      duplicate_check: true,
      links: false,
      pull_requests: false,
      bisect: false,
      incidents: false,
      ci_checks: false,
    },
  },
});

const DOCS = [
  {
    source_uri: "repo-doc://ClickHouse/ClickHouse/merge-tree-design",
    title: "MergeTree merge scheduling — design note",
    description: "Read before touching merge selection or TTL",
    ingested_at: "2026-09-20T10:00:00Z",
    has_blob: true,
    original_filename: "merge-tree.pdf",
  },
  {
    source_uri: "repo-doc://ClickHouse/ClickHouse/oncall-runbook",
    title: "On-call runbook",
    description: "",
    ingested_at: "2026-09-23T08:00:00Z",
  },
];

async function setup(page: Page, opts: { bound?: string | string[]; checkPasses?: boolean } = {}) {
  await installApi(page, { codeRepos: maintainerRepos(opts.bound), trackerSources: sources() });
  const json = (b: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  await page.route("**/v1/knowledge.list", (r) => r.fulfill(json({ rows: DOCS, next_cursor: null })));
  await page.route("**/v1/knowledge.reads", (r) =>
    r.fulfill(
      json({
        window_days: 7,
        reads: [{ source_uri: DOCS[0].source_uri, reads: 12, agents: 3, last_read_at: "2026-09-24 11:00:00" }],
      }),
    ),
  );
  await page.route("**/v1/knowledge.get", (r) =>
    r.fulfill(
      json({
        found: true,
        source_uri: DOCS[0].source_uri,
        title: DOCS[0].title,
        content:
          "# MergeTree merge scheduling\n\nMerges are selected by the SimpleMergeSelector …\n\n## TTL\n\nA part whose rows all expired is dropped by a TTLDrop merge.",
        tags: [],
      }),
    ),
  );
  await page.route("**/v1/tracker.contract", (r) => r.fulfill(json(CONTRACT)));
  await page.route("**/v1/tracker.source.inspect", (r) => r.fulfill(json(inspected(!opts.checkPasses))));
  await page.route("**/v1/tracker.source.add", (r) => {
    const body: unknown = r.request().postDataJSON();
    const dry = !(isRecord(body) && body.dry_run === false);
    return r.fulfill(json(opts.checkPasses ? mapped(dry) : failing(dry)));
  });
  await page.goto("/me/repositories");
  await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
}

async function openCard(page: Page) {
  const c = page.getByRole("region", { name: CODE_REPO, exact: true });
  await c.getByRole("button", { name: CODE_REPO, exact: true }).click();
  await expect(c.getByRole("group", { name: "Docs" })).toBeVisible();
  return c;
}

async function noSidewaysScroll(page: Page) {
  const wide = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const out: string[] = [];
    document.querySelectorAll("body *").forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && r.width > 0 && getComputedStyle(el).position !== "fixed") {
        out.push(
          `${el.tagName.toLowerCase()}.${(el.getAttribute("class") ?? "").slice(0, 60)} right=${Math.round(r.right)}`,
        );
      }
    });
    return { sw: document.documentElement.scrollWidth, vw, out: out.slice(0, 8) };
  });
  expect(wide.sw, JSON.stringify(wide.out)).toBeLessThanOrEqual(wide.vw);
}

for (const [label, viewport] of [
  ["desktop", { width: 1360, height: 900 }],
  ["mobile", { width: 400, height: 860 }],
] as const) {
  test.describe(label, () => {
    test.use({ viewport });

    test(`${label}: page, trackers, a linked card, docs`, async ({ page }) => {
      await setup(page);
      await expect(page.getByRole("heading", { name: "Trackers" })).toBeVisible();
      await shot(page, `${label}-1-page`);
      const c = await openCard(page);
      await expect(c.getByRole("group", { name: "Tracker link" })).toContainText("Linked to qa · public");
      await page.waitForTimeout(200);
      if (label === "mobile") await noSidewaysScroll(page);
      await shot(page, `${label}-2-card-linked`);
      await c.getByRole("button", { name: "MergeTree merge scheduling — design note", exact: true }).click();
      await expect(page.getByRole("dialog")).toContainText("The text agents read");
      await expect(page.getByRole("dialog")).toContainText("opened by agents 12 times in 7 days");
      await page.waitForTimeout(400); // the dialog's fade-in
      await shot(page, `${label}-3-doc-viewer`);
      await page.keyboard.press("Escape");
      await c.getByRole("button", { name: "Add a document" }).click();
      await expect(page.getByRole("dialog")).toContainText("Add a document to ClickHouse/ClickHouse");
      await page.getByLabel("Title").fill("Replication runbook");
      await expect(page.getByRole("dialog")).toContainText("repo-doc://ClickHouse/ClickHouse/replication-runbook");
      await page.waitForTimeout(400);
      await shot(page, `${label}-4-doc-add`);
    });

    test(`${label}: a link that answers nothing is said inline`, async ({ page }) => {
      await setup(page, { bound: "tracker-kj" });
      const c = await openCard(page);
      await expect(c.getByRole("group", { name: "Tracker link" })).toContainText("agents get nothing from it here");
      await c.getByRole("button", { name: "Link another tracker" }).click();
      await page.waitForTimeout(150);
      await shot(page, `${label}-5-card-link-warn`);
    });

    test(`${label}: a repository reading two trackers says which is primary`, async ({ page }) => {
      await setup(page, { bound: ["tracker-clickhouse", "tracker-kj"] });
      const c = await openCard(page);
      const g = c.getByRole("group", { name: "Tracker link" });
      await expect(g.getByRole("listitem")).toHaveCount(2);
      await expect(g).toContainText("Agents search all 2; a lookup by number");
      await expect(g.getByRole("button", { name: "Make kj primary" })).toBeVisible();
      if (label === "mobile") await noSidewaysScroll(page);
      await g.scrollIntoViewIfNeeded();
      await page.waitForTimeout(150);
      await g.screenshot({ path: path.join(SHOTS, `${TAG}-${label}-5b-card-two-trackers.png`) });
    });

    test(`${label}: add a tracker — connect, a views database, check failing with the fix`, async ({ page }) => {
      await setup(page);
      const c = await openCard(page);
      await c.getByRole("button", { name: /Add a tracker/ }).click();
      const d = page.getByRole("dialog");
      await d
        .getByLabel(/ClickHouse address/)
        .fill("https://abc.us-east-1.aws.clickhouse.cloud:8443/?database=loom_tracker");
      await d.getByLabel(/Read-only user/).fill("loom_ro");
      await d.getByLabel(/^Password/).fill("secret");
      await expect(d.getByText("abc.us-east-1.aws.clickhouse.cloud", { exact: true })).toBeVisible();
      await shot(page, `${label}-6-add-connect`);
      await d.getByRole("button", { name: /Next: map your tables/ }).click();
      await expect(d.getByText(/already exposes Loom's tables — nothing to map/)).toBeVisible();
      await shot(page, `${label}-7-add-views-detected`);
      await d.getByRole("button", { name: /^Check/ }).click();
      await expect(d.getByText(/Fix the tables below/)).toBeVisible();
      await expect(d.getByRole("button", { name: /Add and link/ })).toBeDisabled();
      if (label === "mobile") await noSidewaysScroll(page);
      await shot(page, `${label}-8-add-check-failing`);
    });

    test(`${label}: a passing check adds and says what happened`, async ({ page }) => {
      await setup(page, { checkPasses: true });
      await page.getByRole("button", { name: "Add a tracker" }).first().click();
      const d = page.getByRole("dialog");
      await d.getByLabel(/ClickHouse address/).fill("abc.clickhouse.cloud/?database=helpdesk");
      await d.getByLabel("Repositories it holds").fill("Acme/widgets,");
      await d.getByLabel(/Read-only user/).fill("loom_ro");
      await d.getByLabel(/^Password/).fill("secret");
      await d.getByRole("button", { name: /Next: map your tables/ }).click();
      await expect(d.getByLabel("Items: your table")).toHaveValue("tickets");
      await expect(d.getByLabel(/Reference: number/)).toHaveValue("digits:ticket_key");
      if (label === "mobile") await noSidewaysScroll(page);
      await shot(page, `${label}-8b-add-map`);
      await d.getByRole("button", { name: /^Check/ }).click();
      await expect(d.getByRole("region", { name: "What Loom can do with this tracker" })).toBeVisible();
      await expect(d.getByText("Ready to add. Nothing has been saved yet.")).toBeVisible();
      await shot(page, `${label}-9-add-check-passing`);
      await d.getByRole("button", { name: "Add tracker" }).click();
      await expect(d.getByText(/is connected/)).toBeVisible();
      await shot(page, `${label}-10-add-done`);
    });
  });
}
