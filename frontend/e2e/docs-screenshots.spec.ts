// Documentation screenshots of the / dashboard: the production build (`vite preview`, as
// every spec here) with every `/v1/*` call answered from the invented demo data below, one
// small team "acme" — never a live server. The spec is skipped unless
// LOOM_DOCS_SCREENSHOTS=1, so the suite's own run never writes into docs/. Regenerate the
// PNGs in docs/images/ from frontend/ with:
//
//   LOOM_DOCS_SCREENSHOTS=1 E2E_PORT=4271 npx playwright test e2e/docs-screenshots.spec.ts
//
// The clock is fixed, so relative times ("2h ago") read the same on every run.

import { expect, test, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { isRecord, pickOption } from "./fixtures";

const ENABLED = process.env.LOOM_DOCS_SCREENSHOTS === "1";
const OUT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../../docs/images");
const NOW = Date.parse("2026-09-28T15:00:00Z");

const ORG = "acme";
const NS = "acme-platform";
const NS_SUPPORT = "acme-support";
const CODE_NS = "code-acme-payments";
const CODE_NS_RELEASE = "code-acme-payments-release";
const TRACKER_NS = "tracker-acme";
const SUBJECT = "dev@acme.example";

/** ISO time `h` hours before the fixed clock. */
const ago = (h: number) => new Date(NOW - h * 3600_000).toISOString().replace(/\.\d{3}Z$/, "Z");
/** The same, in ClickHouse's `YYYY-MM-DD hh:mm:ss` form. */
const agoCh = (h: number) => ago(h).slice(0, 19).replace("T", " ");

interface DemoMemory {
  id: string;
  key: string;
  type: string;
  value: string;
  tags: string[];
  agent: string;
  hours: number;
  pinned?: boolean;
  verified?: boolean;
  version?: number;
  status?: string;
  entities?: string[];
}

const MEMORIES: DemoMemory[] = [
  {
    id: "3f9a2c71-1111-4a1e-9a51-3f9a2c710001",
    key: "deploys/policy/windows",
    type: "semantic",
    value: "Deploys run Tuesday and Thursday only. The freeze starts Friday 12:00 UTC and lifts Monday 09:00 UTC.",
    tags: ["deploys", "policy"],
    agent: "claude-code",
    hours: 3.5,
    pinned: true,
    verified: true,
    version: 3,
    entities: ["deploys", "release-train"],
  },
  {
    id: "b41e07d3-2222-4a1e-9a51-b41e07d30002",
    key: "staging/db/read-only-window",
    type: "semantic",
    value: "Staging DB is read-only after 18:00 UTC while the nightly snapshot restores; writes resume at 18:40 UTC.",
    tags: ["staging", "database"],
    agent: "claude-code",
    hours: 2,
    entities: ["staging-db", "nightly-snapshot"],
  },
  {
    id: "e8c5d920-3333-4a1e-9a51-e8c5d9200003",
    key: "api/rate-limit/public",
    type: "semantic",
    value: "The public API allows 1,000 requests per minute per key since the v3 gateway rollout; burst is 100.",
    tags: ["api", "limits"],
    agent: "support-bot",
    hours: 1.5,
    version: 2,
    status: "conflicted",
    entities: ["public-api", "v3-gateway"],
  },
  {
    id: "5a0b6f14-4444-4a1e-9a51-5a0b6f140004",
    key: "support/faq/api-limits",
    type: "semantic",
    value: "Customers asking about limits: the public API allows 600 requests per minute per key; burst is 50.",
    tags: ["support", "api", "limits"],
    agent: "support-bot",
    hours: 190,
    status: "conflicted",
    entities: ["public-api"],
  },
  {
    id: "c27d93ae-5555-4a1e-9a51-c27d93ae0005",
    key: "procedures/rollback-prod",
    type: "procedural",
    value:
      "Roll back production: 1) pause the deploy pipeline, 2) run `make rollback ENV=prod`, 3) confirm /healthz is green on every region, 4) post the SHA in #deploys.",
    tags: ["runbook", "deploys"],
    agent: "claude-code",
    hours: 26,
    verified: true,
    entities: ["deploys", "rollback"],
  },
  {
    id: "91f4e6b8-6666-4a1e-9a51-91f4e6b80006",
    key: "support/refunds/approval",
    type: "semantic",
    value: "Refunds over $500 need a second approver in the billing console before they are issued.",
    tags: ["support", "billing"],
    agent: "support-bot",
    hours: 5,
    entities: ["refunds", "billing-console"],
  },
  {
    id: "4c9e1d72-1212-4a1e-9a51-4c9e1d720011",
    key: "billing/refunds/threshold",
    type: "semantic",
    value: "Refunds over $250 need a second approver; the billing console enforces it.",
    tags: ["billing"],
    agent: "support-bot",
    hours: 8,
    status: "conflicted",
    entities: ["refunds", "billing-console"],
  },
  {
    id: "0d6ac357-7777-4a1e-9a51-0d6ac3570007",
    key: "incidents/checkout-latency",
    type: "episodic",
    value:
      "Checkout p95 latency doubled after Monday's deploy; traced to an N+1 query in cart totals and fixed in acme/payments#482.",
    tags: ["incident", "payments"],
    agent: "claude-code",
    hours: 30,
    entities: ["checkout", "acme/payments"],
  },
  {
    id: "f3b812c9-8888-4a1e-9a51-f3b812c90008",
    key: "prefs/git/merge-style",
    type: "preference",
    value: "Prefer squash merges; PR titles use the imperative mood and name the service first.",
    tags: ["git", "conventions"],
    agent: "claude-code",
    hours: 72,
  },
  {
    id: "6e27a4d0-9999-4a1e-9a51-6e27a4d00009",
    key: "ci/cache/node-modules",
    type: "semantic",
    value: "CI caches node_modules keyed on package-lock.json; bump CACHE_VERSION in ci.yml to bust it.",
    tags: ["ci"],
    agent: "claude-code",
    hours: 96,
  },
  {
    id: "a85c1f6b-1010-4a1e-9a51-a85c1f6b0010",
    key: "support/tone/sign-off",
    type: "preference",
    value: "Support replies sign off as “The Acme team”, never with a personal name.",
    tags: ["support", "tone"],
    agent: "support-bot",
    hours: 120,
  },
];

const byKey = (k: string) => MEMORIES.find((m) => m.key === k);

function listEntry(m: DemoMemory) {
  return {
    memory_id: m.id,
    memory_key: m.key,
    memory_type: m.type,
    confidence: 0.95,
    version: m.version ?? 1,
    created_at: ago(m.hours),
    updated_at: ago(m.hours),
    expires_at: null,
    tombstoned_at: null,
    pinned: Boolean(m.pinned),
    tags: m.tags,
    agent: m.agent,
    user_id: "-",
    session_id: "-",
    namespace: NS,
    org: ORG,
    verification: m.verified ? "human" : "none",
    verified_by: m.verified ? `human:${SUBJECT}` : "",
    value: m.value,
    value_truncated: false,
    content_sha256: "5f3a".repeat(16),
  };
}

function detail(m: DemoMemory) {
  return {
    ...listEntry(m),
    found: true,
    status: m.status ?? "active",
    source_trace: m.agent === "support-bot" ? "conversation:thr-2f9c" : "session:claude-code",
    recall_count: 14,
    last_recalled_at: ago(0.4),
    recall_by_agent: [
      { agent: "claude-code", count: 11, last_at: ago(0.4) },
      { agent: "support-bot", count: 3, last_at: ago(6) },
    ],
    entities: m.entities ?? [],
    char_limit: null,
    verified_at: m.verified ? ago(m.hours - 1) : null,
  };
}

function must(key: string): DemoMemory {
  const m = byKey(key);
  if (!m) throw new Error(`no demo memory ${key}`);
  return m;
}

/** Each conflicted row and the older peer it disagrees with. */
const CONFLICT_PAIRS: Array<[DemoMemory, DemoMemory]> = [
  [must("api/rate-limit/public"), must("support/faq/api-limits")],
  [must("billing/refunds/threshold"), must("support/refunds/approval")],
];

function conflictRow(a: DemoMemory, b: DemoMemory) {
  return {
    memory_id: a.id,
    memory_key: a.key,
    memory_type: a.type,
    value: a.value,
    version: a.version ?? 1,
    pinned: false,
    created_at: ago(a.hours),
    conflict_with: [b.id],
    content_sha256: "5f3a".repeat(16),
  };
}

const LEARNINGS = [
  {
    candidate_id: "lc-7a31",
    memory_key: "deploys/policy/hotfix-exception",
    memory_type: "semantic",
    proposed_value: "Hotfixes may ship outside the deploy window with the incident commander's sign-off.",
    supporter_count: 3,
    confidence_avg: 0.86,
    supporters: ["claude-code", "support-bot"],
    rationale: "Stated in three sessions this week; no memory covers the exception yet.",
    status: "pending",
    proposed_at: ago(4),
    decided_at: null,
    decided_by: "",
    decision_reason: "",
  },
];

const MINE = [
  {
    namespace: NS,
    role: "admin",
    status: "active",
    visibility: "private",
    kind: "memory",
    description: "Platform team memory",
    joined_at: ago(900),
    is_new: false,
  },
  {
    namespace: NS_SUPPORT,
    role: "writer",
    status: "active",
    visibility: "private",
    kind: "memory",
    description: "Support bot memory",
    joined_at: ago(800),
    is_new: false,
  },
  {
    namespace: CODE_NS,
    role: "viewer",
    status: "active",
    visibility: "private",
    kind: "code",
    description: "acme/payments @ main",
    joined_at: ago(700),
    is_new: false,
  },
  {
    namespace: CODE_NS_RELEASE,
    role: "viewer",
    status: "active",
    visibility: "private",
    kind: "code",
    description: "acme/payments @ release-2026.09",
    joined_at: ago(300),
    is_new: false,
  },
  {
    namespace: TRACKER_NS,
    role: "viewer",
    status: "active",
    visibility: "private",
    kind: "qa",
    description: "acme/payments issues and PRs",
    joined_at: ago(600),
    is_new: false,
  },
];

function codeRepo(namespace: string, branch: string, head: string, files: number, symbols: number, hours: number) {
  return {
    org: ORG,
    namespace,
    repo: "acme/payments",
    branch,
    git_head: head,
    clone_head: head,
    fast_tier_lag: 0,
    scip_lag: 0,
    scip_py_lag: 0,
    scip_producer: "scip-typescript",
    indexed_at: agoCh(hours),
    last_refresh_at: agoCh(hours / 3),
    counts: { code_files: files, code_symbols: symbols },
    readable: true,
    join_policy: "open",
    my_status: "member",
    my_role: "viewer",
    source: "registry",
    classification: "standard",
    unreadable_reason: "",
    stage: "ready",
    stage_detail: "",
    chunks_total: files * 7,
    chunks_embedded: files * 7,
    embedding_coverage: 1,
    searchable: true,
    docs_namespace: "",
    bound_trackers: [TRACKER_NS],
  };
}

const CODE_REPOS = [
  codeRepo(CODE_NS, "main", "4be19a0c7d2e5f13", 1284, 18342, 0.3),
  codeRepo(CODE_NS_RELEASE, "release-2026.09", "9c02e7b1a4f86d25", 1261, 17990, 20),
];

/** The team's issue tracker, read live; it covers the one repository. */
const TRACKER_SOURCES = [
  {
    name: "acme-issues",
    host: "tracker.acme.example",
    port: 9440,
    database: "issues",
    configured: true,
    origin: "registry",
    contract: {
      ok: true,
      summary: "every table and column present",
      checked_at: ago(0.5),
      optional_present: [],
      features: {},
    },
    breaker: { state: "CLOSED", consecutive_failures: 0 },
    freshness: { issues: 1873, prs: 4210, last_refresh_at: agoCh(0.2) },
    namespaces: [
      {
        namespace: TRACKER_NS,
        kind: "qa",
        visibility: "private",
        repos: ["acme/payments"],
        classification: "standard",
        join_policy: "open",
        registered: true,
        member_role: "viewer",
        pending_role: "",
      },
    ],
  },
];

/** The conflicted row's two versions: the support bot's newer figure replaced the old one. */
function historyFor(key: string) {
  const m = byKey(key);
  if (!m) return [];
  if (key === "api/rate-limit/public")
    return [
      {
        version: 2,
        value: m.value,
        created_at: ago(m.hours),
        tombstoned_at: null,
        redacted_at: null,
        source_trace: "conversation:thr-2f9c",
      },
      {
        version: 1,
        value: "The public API allows 600 requests per minute per key; burst is 50.",
        created_at: ago(310),
        tombstoned_at: null,
        redacted_at: null,
        source_trace: "session:claude-code",
      },
    ];
  return [
    {
      version: m.version ?? 1,
      value: m.value,
      created_at: ago(m.hours),
      tombstoned_at: null,
      redacted_at: null,
      source_trace: "",
    },
  ];
}

function relatedFor(key: string) {
  if (key === "api/rate-limit/public")
    return [
      { memory_key: "support/faq/api-limits", shared_entities: ["public-api"], overlap: 1 },
      { memory_key: "deploys/policy/windows", shared_entities: ["v3-gateway"], overlap: 1 },
    ];
  return [];
}

function provenanceFor(key: string) {
  const m = byKey(key);
  if (!m) return { found: false };
  return {
    found: true,
    source_trace: "conversation:thr-2f9c",
    source_kind: "conversation",
    source_label: "Support conversation",
    thread_id: "thr-2f9c",
    thread_exists: true,
    thread_title: "Customer asks why their bulk import hits 429s",
    agent: m.agent,
    session_id: "sess-81b2",
    run_id: "",
    written_at: ago(m.hours),
    source_resource_id: "docs/api/limits.md",
    links: [],
  };
}

/** `memory.search` hits for the Recall view's query, best first. */
function recallHitsFor(query: string) {
  if (!query) return [];
  const rows: Array<[string, number, number]> = [
    ["deploys/policy/windows", 0.83, 0.06],
    ["procedures/rollback-prod", 0.71, 0.04],
    ["incidents/checkout-latency", 0.58, 0.02],
    ["ci/cache/node-modules", 0.44, 0],
    ["staging/db/read-only-window", 0.39, 0],
  ];
  return rows.map(([key, sim, boost]) => {
    const m = byKey(key);
    return {
      memory_key: key,
      memory_type: m?.type ?? "semantic",
      content_excerpt: m?.value ?? "",
      distance: 1 - sim,
      similarity: sim,
      rank_score: sim + boost,
      confidence: 1,
      score_components: { entity_ann_boost: boost },
      source: "fact",
    };
  });
}

const SERVICE_ACCOUNTS = [
  {
    name: "claude-code-laptop",
    sa_subject: "sa:claude-code-laptop-4f2a",
    namespaces: [NS, "*"],
    roles: ["writer"],
    ttl_days: 90,
    created_by: SUBJECT,
    created_at: ago(400),
    expires_at: ago(-1760),
    revoked_at: null,
    revoked_by: "",
    last_used_at: ago(0.2),
    ns_roles: {},
  },
  {
    name: "support-bot",
    sa_subject: "sa:support-bot-91c3",
    namespaces: [NS_SUPPORT],
    roles: ["writer"],
    ttl_days: 365,
    created_by: SUBJECT,
    created_at: ago(800),
    expires_at: ago(-7960),
    revoked_at: null,
    revoked_by: "",
    last_used_at: ago(1.1),
    ns_roles: { [NS_SUPPORT]: "writer" },
  },
];

const CREATED_TOKEN = {
  token: "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJkZXZAYWNtZS5leGFtcGxlIn0.demo",
  name: "token-2026-01-01-2b7e01",
  sa_subject: "sa:token-2026-01-01-2b7e01-9f3a1c",
  org: ORG,
  namespaces: [NS, "*"],
  ns_roles: {},
  reads_everything: true,
  saves_to: NS,
  created_at: "2026-01-01T00:00:00",
  expires_at: null,
  restricted_namespaces: [],
  mcp_url: "https://loom.acme.example/mcp",
};

const debugCalls: string[] = [];

async function installDemoApi(page: Page): Promise<void> {
  await page.clock.setFixedTime(NOW);
  await page.addInitScript(() => {
    localStorage.setItem("loom_token_v1", "demo-token");
    localStorage.setItem("loom_namespace_v1", "acme-platform");
  });
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
    const p = url.pathname;
    switch (p) {
      case "/v1/whoami":
        return json({
          authenticated: true,
          subject: SUBJECT,
          org: ORG,
          namespaces: [NS, NS_SUPPORT],
          roles: ["PLATFORM_ADMIN"],
          is_service_account: false,
        });
      case "/v1/namespace.list_mine":
        return json({ rows: MINE });
      case "/v1/memory.list": {
        if (body.count_only) return json({ total: MEMORIES.length });
        if (body.memory_type === "profile") return json({ entries: [], total: 0, today: 0, truncated: false });
        // Newest first, as the server orders the page; `contains` keeps the rows whose key
        // or value holds the text, any case.
        const contains = String(body.contains ?? "").toLowerCase();
        const rows = [...MEMORIES]
          .filter((m) => !contains || `${m.key} ${m.value}`.toLowerCase().includes(contains))
          .sort((x, y) => x.hours - y.hours)
          .map(listEntry);
        return json({
          entries: rows,
          total: rows.length,
          today: MEMORIES.filter((m) => m.hours < 15).length,
          truncated: false,
          namespace_version: "1",
          unchanged: false,
        });
      }
      case "/v1/memory.get": {
        const m = byKey(String(body.memory_key ?? ""));
        return json(m ? detail(m) : { found: false });
      }
      case "/v1/memory.get_by_id": {
        const m = MEMORIES.find((x) => x.id === String(body.memory_id ?? ""));
        return json(m ? detail(m) : { found: false });
      }
      case "/v1/memory.bulk_get": {
        const keys = Array.isArray(body.memory_keys) ? body.memory_keys.map(String) : [];
        const results: Record<string, unknown> = {};
        const missing: string[] = [];
        for (const k of keys) {
          const m = byKey(k);
          if (m) results[k] = detail(m);
          else missing.push(k);
        }
        return json({ results, missing });
      }
      case "/v1/conflicts.list": {
        const rows = CONFLICT_PAIRS.map(([a, b]) => conflictRow(a, b));
        if (body.count_only) return json({ total: rows.length });
        if (body.keys_only) return json({ keys: CONFLICT_PAIRS.flat().map((m) => m.key) });
        return json({ rows, next_before_version: null });
      }
      case "/v1/learnings.list":
        return json({ rows: body.status === "pending" ? LEARNINGS : [] });
      case "/v1/conflicts.allowlist_list":
        return json({ rows: [] });
      case "/v1/memory.history":
        return json({ rows: historyFor(String(body.memory_key ?? "")) });
      case "/v1/memory.related":
        return json({ rows: relatedFor(String(body.memory_key ?? "")) });
      case "/v1/memory.lineage":
        return json({ steps: [] });
      case "/v1/memory.provenance":
        return json(provenanceFor(String(body.memory_key ?? "")));
      case "/v1/memory.search":
        return json({
          results: recallHitsFor(String(body.query ?? "")),
          embedding_model: "text-embedding-3-small",
        });
      case "/v1/memory.search_trace_log":
        return json({ rows: [] });
      case "/v1/me/service_accounts":
        return json({ rows: SERVICE_ACCOUNTS });
      case "/v1/me/service_accounts.create":
        return json(CREATED_TOKEN);
      case "/v1/tracker.source.list":
        return json({ sources: TRACKER_SOURCES, self_service: true });
      case "/v1/code.repos":
        return json({ repos: CODE_REPOS });
      case "/v1/me/sharing-summary":
        return json({
          subject: SUBJECT,
          org: ORG,
          window_days: 7,
          totals: { writes: 0, recalls: 0, cross_namespace_recalls: 0, namespaces: 0, agents: 0 },
          writes: 0,
          recalls: 0,
          cross_namespace_recalls: 0,
          namespaces: [],
          agents: [],
          agent_namespaces: [],
        });
      case "/v1/cost.budget_status":
        return json({
          namespace: NS,
          spent_24h_usd: 0.42,
          cap_usd: 0,
          cap_source: "uncapped",
          over_cap: false,
          pct_of_cap: null,
          namespaces_with_spend: 1,
        });
      case "/v1/ui.vitals":
        return json({ recorded: true, route: body.route ?? "" });
      default:
        break;
    }
    debugCalls.push(`${p} ${JSON.stringify(body).slice(0, 300)}`);
    return json({ rows: [], entries: [], total: 0, today: 0, truncated: false });
  });
}

async function capture(page: Page, name: string): Promise<void> {
  fs.mkdirSync(OUT, { recursive: true });
  // Let fonts, transitions and the after-paint mounts settle before the frame.
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

test.describe("docs screenshots", () => {
  test.skip(!ENABLED, "set LOOM_DOCS_SCREENSHOTS=1 to regenerate docs/images");
  test.use({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2, colorScheme: "dark" });

  test.afterEach(() => {
    if (process.env.LOOM_DOCS_DEBUG) console.log(debugCalls.join("\n"));
    debugCalls.length = 0;
  });

  test("memories", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/memories");
    await expect(page.getByText("deploys/policy/windows").first()).toBeVisible();
    // The list, not the audit and mentor panels above it, is the subject.
    await page.getByPlaceholder(/Search memories/).evaluate((el) => {
      // A little air between the header and the search box.
      (el as HTMLElement).style.scrollMarginTop = "14px";
      el.scrollIntoView({ block: "start" });
    });
    await capture(page, "dashboard-memories");
  });

  test("memory detail", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/memories");
    await page.getByText("api/rate-limit/public", { exact: true }).first().click();
    const drawer = page.getByRole("dialog");
    await expect(drawer.getByText("Conflicts with 1 other memory")).toBeVisible();
    await drawer.getByRole("button", { name: /History/ }).click();
    await drawer.getByRole("button", { name: /Provenance/ }).click();
    const older = drawer.getByRole("button", { name: /^v1\b/ });
    await older.click();
    await older.evaluate((el) => el.scrollIntoView({ block: "center" }));
    await capture(page, "dashboard-memory-detail");
  });

  test("review queue", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/admin/review_queue");
    await expect(page.getByText("Conflict 1 of 2")).toBeVisible();
    await capture(page, "dashboard-review-queue");
  });

  test("recall", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/recall");
    const input = page.getByLabel("Recall query");
    await input.fill("when can I deploy to production?");
    await expect(page.getByText(/5 rows/)).toBeVisible();
    await input.blur();
    await capture(page, "dashboard-recall");
  });

  test("repositories", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/repositories");
    const toggle = page.getByRole("button", { name: "acme/payments", exact: true });
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await capture(page, "dashboard-repositories");
  });

  test("connect a client", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/settings");
    await page.getByRole("button", { name: "New token" }).click();
    await expect(page.getByRole("radio", { name: "Everything you belong to" })).toBeChecked();
    await capture(page, "dashboard-new-token");
    await page.getByRole("button", { name: "Create token" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("Token created")).toBeVisible();
    await pickOption(dialog, "Connect", "claude-code");
    await expect(dialog.getByTestId("claude-code-recommended")).toBeVisible();
    await capture(page, "dashboard-connect");
  });
});
