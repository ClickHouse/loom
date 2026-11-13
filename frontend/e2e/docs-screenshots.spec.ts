// Documentation screenshots of the / dashboard: the production build (`vite preview`, as
// every spec here) with every `/v1/*` call answered from the invented demo data below, one
// small team "acme" — never a live server. The spec is skipped unless
// LOOM_DOCS_SCREENSHOTS=1, so the suite's own run never writes into docs/. Regenerate the
// PNGs in docs/images/ from frontend/ with:
//
//   LOOM_DOCS_SCREENSHOTS=1 E2E_PORT=4271 npx playwright test e2e/docs-screenshots.spec.ts
//
// The clock is fixed, so relative times ("2h ago") read the same on every run. The 1440 px
// shots go to docs/assets/screenshots/ as WebP, encoded by Chromium; the clips go there as
// WebM, recorded by Playwright.

import { expect, test, type Browser, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { isRecord, pickOption } from "./fixtures";

const ENABLED = process.env.LOOM_DOCS_SCREENSHOTS === "1";
const OUT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../../docs/images");
// The 1440 px shots (WebP) and the clips (WebM). The PNGs above stay where they are: the
// English pages link them, and the translated READMEs link the memories PNG by URL.
const OUT_ASSETS = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "../../docs/assets/screenshots");
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
  /** The code the memory is linked to (`memory.get`'s `code`); `state` takes CodeLink's
   *  values (frontend/src/api.ts). */
  code?: Array<{
    path: string;
    symbol: string;
    state: "fresh" | "moved" | "changed" | "unresolved" | "missing";
    changed_since?: string;
  }>;
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
    id: "7b2e9c40-1313-4a1e-9a51-7b2e9c400013",
    key: "payments/fx/rounding",
    type: "semantic",
    value:
      "FX conversion rounds half to even, as the settlement bank does; never switch `payments.fx.convert` to ROUND_HALF_UP, or `payments.refunds.refund` stops matching settlement.",
    // The repository tag the coding plugins put on a session's memories, which the code-link
    // worker links from.
    tags: ["payments", "constraint", "repo:5e1a9c03d2"],
    agent: "claude-code",
    hours: 50,
    verified: true,
    entities: ["fx", "settlement"],
    code: [
      { path: "payments/fx.py", symbol: "payments.fx.convert", state: "fresh" },
      {
        path: "payments/refunds.py",
        symbol: "payments.refunds.refund",
        state: "changed",
        changed_since: "2d71c9e0b5a3f846",
      },
    ],
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
    code: (m.code ?? []).map((c) => ({ code_namespace: CODE_NS, changed_since: "", suggestion: "", ...c })),
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

/** `code.coverage` for the main branch: Python and TypeScript, fully embedded. */
function coverage() {
  const lang = (name: string, files: number, symbols: number) => ({
    lang: name,
    files,
    files_errored: 0,
    files_first_at: agoCh(900),
    files_last_at: agoCh(0.4),
    chunks: files * 7,
    chunks_embedded: files * 7,
    chunks_described: files,
    chunks_last_at: agoCh(0.4),
    symbols_treesitter: symbols,
    symbols_treesitter_last_at: agoCh(0.4),
    symbols_scip: symbols,
    symbols_scip_last_at: agoCh(0.4),
  });
  return {
    org: ORG,
    namespace: CODE_NS,
    as_of_commit: "4be19a0c7d2e5f13",
    languages: [lang("python", 1042, 15880), lang("typescript", 242, 2462)],
    tiers: [{ tier: "scip", edges: 52110, edges_last_at: agoCh(0.4) }],
    gaps: null,
    pr_history_from: "2024-01-03",
    pr_history_to: "2026-09-28",
    pr_history_last_fetch_at: agoCh(0.5),
    pr_history_enabled: true,
    pr_history_auth: "token",
    context_packs_last_at: agoCh(2),
    summaries_last_at: agoCh(3),
  };
}

/** `code.index_status` for the main branch: every stage at the tip, each worker healthy. */
function indexStatus() {
  const worker = (minutes: number) => ({
    enabled: true,
    interval_seconds: 60,
    last_run_at: ago(minutes / 60),
    last_duration_ms: 2400,
    consecutive_failures: 0,
    total_runs: 4120,
    last_error: "",
  });
  const head = "4be19a0c7d2e5f13";
  const base = codeRepo(CODE_NS, "main", head, 1284, 18342, 0.3);
  return {
    ...base,
    counts: {
      ...base.counts,
      code_chunks: 8988,
      code_edges: 52110,
      code_summaries: 1284,
      code_pr_history: 4210,
      code_context_packs: 37,
    },
    scip_commit: head,
    scip_commit_py: head,
    compiler_tiers: [],
    pr_cursor: "2026-09-28",
    last_refresh_detail: "",
    path_prefixes: [],
    provider: "github",
    profile: "python",
    compiler_candidates: [],
    quality_warnings: [],
    state: {
      git_head: { value: head, detail: "", updated_at: agoCh(0.3) },
      pr_cursor: { value: "2026-09-28", detail: "", updated_at: agoCh(0.5) },
    },
    workers: {
      code_ingest: worker(2),
      code_embed_recovery: worker(3),
      code_graph_stats: worker(9),
      code_pr_history: worker(11),
      code_summary: worker(14),
    },
  };
}

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
  roles: ["writer"],
  reads_everything: true,
  saves_to: NS,
  created_at: "2026-01-01T00:00:00",
  expires_at: null,
  restricted_namespaces: [],
  mcp_url: "https://loom.acme.example/mcp",
};

/** The Library's documents: an on-call runbook, the refunds policy and the FX note. */
const DOCUMENTS = [
  {
    source_uri: "runbooks/payments-on-call.md",
    title: "Payments on-call runbook",
    description: "What to check first when checkout or refunds page someone.",
    tags: ["runbook", "payments"],
    hours: 30,
    chunks: 14,
  },
  {
    source_uri: "policies/refunds.md",
    title: "Refunds policy",
    description: "Who may refund what, approvals, and how refunds settle.",
    tags: ["policy", "billing"],
    hours: 74,
    chunks: 9,
  },
  {
    source_uri: "architecture/fx-settlement.md",
    title: "FX and settlement",
    description: "How converted amounts are rounded and reconciled with the settlement bank.",
    tags: ["architecture", "payments"],
    hours: 160,
    chunks: 6,
  },
];

function documentRow(d: (typeof DOCUMENTS)[number]) {
  return {
    source_uri: d.source_uri,
    title: d.title,
    description: d.description,
    tags: d.tags,
    ingested_at: ago(d.hours),
    chunk_count: d.chunks,
    superseded: false,
    has_blob: false,
    content_preview: d.description,
  };
}

/** `knowledge.search` passages for the refund question, best first, each with where it sits. */
const PASSAGES = [
  {
    source_uri: "policies/refunds.md",
    chunk_idx: 3,
    section: "Approvals",
    content:
      "A refund over $250 needs a second approver in the billing console before it is issued. The approver must not be the person who took the original payment.",
    score: 0.86,
    similarity: 0.86,
    distance: 0.14,
    tags: [],
    revision: 4,
    format: "markdown",
    locator: { line_start: 41, line_end: 52 },
  },
  {
    source_uri: "runbooks/payments-on-call.md",
    chunk_idx: 7,
    section: "Refund stuck in pending",
    content:
      "If a refund stays pending for more than an hour, check the settlement queue first; a refund in a currency other than USD waits for the day's FX rate.",
    score: 0.74,
    similarity: 0.74,
    distance: 0.26,
    tags: [],
    revision: 2,
    format: "markdown",
    locator: { line_start: 118, line_end: 131 },
  },
  {
    source_uri: "architecture/fx-settlement.md",
    chunk_idx: 1,
    section: "Rounding",
    content:
      "Converted amounts round half to even, the rule the settlement bank applies, so a refund in EUR settles to the cent the bank reports.",
    score: 0.61,
    similarity: 0.61,
    distance: 0.39,
    tags: [],
    revision: 1,
    format: "markdown",
    locator: { line_start: 12, line_end: 20 },
  },
];

const AGENT_SUBJECT = "sa:claude-code-7f2a";
const AGENT_WRITER = `process:${AGENT_SUBJECT}`;
const BOT_WRITER = "process:sa:support-bot-91c3";
/** The Audit log's events, with the subject fields the Audit log's labels read. A memory
 *  write names its writer as `kind:subject`; any other audited op names the token's
 *  subject. Each memory write is as old as the memory it saved. */
const AUDIT_ROWS = [
  { h: 1.5, event_type: "memory.set", actor: BOT_WRITER, subject: { memory_key: "api/rate-limit/public" } },
  { h: 2.0, event_type: "memory.set", actor: AGENT_WRITER, subject: { memory_key: "staging/db/read-only-window" } },
  {
    h: 3.1,
    event_type: "grants.create",
    actor: SUBJECT,
    subject: { role: "writer", namespace: NS_SUPPORT, subject: "sa:support-bot-91c3" },
  },
  { h: 3.5, event_type: "memory.set", actor: AGENT_WRITER, subject: { memory_key: "deploys/policy/windows" } },
  { h: 4.6, event_type: "memory.delete", actor: AGENT_SUBJECT, subject: { memory_key: "staging/db/legacy-host" } },
  { h: 5.0, event_type: "memory.set", actor: BOT_WRITER, subject: { memory_key: "support/refunds/approval" } },
  { h: 6.7, event_type: "memory.promote", actor: SUBJECT, subject: { memory_key: "procedures/rollback-prod" } },
  { h: 8.2, event_type: "namespace.join", actor: "dev2@acme.example", subject: { namespace: NS, role: "viewer" } },
  { h: 12.0, event_type: "knowledge.delete", actor: SUBJECT, subject: { source_uri: "runbooks/old-pager-rota.md" } },
  {
    h: 20.0,
    event_type: "grants.revoke",
    actor: SUBJECT,
    subject: { subject: "sa:ci-bot-3d1e", namespace: NS_SUPPORT },
  },
  { h: 30.0, event_type: "memory.set", actor: AGENT_WRITER, subject: { memory_key: "incidents/checkout-latency" } },
  { h: 50.0, event_type: "memory.set", actor: AGENT_WRITER, subject: { memory_key: "payments/fx/rounding" } },
].map(({ h, event_type, actor, subject }) => ({
  ts: ago(h),
  event_type,
  actor,
  subject: JSON.stringify(subject),
}));

/** The acme/payments code the Explore tab reads: FX conversion, the refund that calls it, and
 *  the API handler that calls the refund. */
const CODE_UNITS: Record<string, Record<string, unknown>> = {
  "payments.fx.convert": {
    path: "payments/fx.py",
    start_line: 8,
    end_line: 12,
    kind: "function",
    signature: "def convert(amount: Decimal, source: str, target: str) -> Decimal",
    description: "Currency conversion at the rate of the settlement day, rounded half to even.",
    body: 'def convert(amount: Decimal, source: str, target: str) -> Decimal:\n    if source == target:\n        return amount\n    rate = RATES[(source, target)]\n    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)',
    churn: 3,
    amp: 2,
    tested: true,
  },
  "payments.refunds.refund": {
    path: "payments/refunds.py",
    start_line: 9,
    end_line: 11,
    kind: "function",
    signature: 'def refund(charge_id: str, amount: Decimal, currency: str, settle_in: str = "USD") -> None',
    description: "Reverses a captured charge and posts the balanced ledger entry.",
    body: 'def refund(charge_id: str, amount: Decimal, currency: str, settle_in: str = "USD") -> None:\n    settled = convert(amount, currency, settle_in)\n    post([Entry(f"customer:{charge_id}", settled, settle_in), Entry("revenue", -settled, settle_in)])',
    churn: 5,
    amp: 1,
    tested: true,
  },
  "payments.api.handle_refund": {
    path: "payments/api.py",
    start_line: 8,
    end_line: 10,
    kind: "function",
    signature: "def handle_refund(body: dict) -> dict",
    description: "The POST /refunds handler.",
    body: 'def handle_refund(body: dict) -> dict:\n    refund(body["charge_id"], Decimal(body["amount"]), body["currency"])\n    return {"status": "refunded"}',
    churn: 2,
    amp: 0,
    tested: false,
  },
};

/** A definition as `code.symbol` answers it: a symbol unit, with the compiler tier and the
 *  signature, and none of the ranking annotations (churn, amp, tested) a search result has. */
function symbolUnit(symbol: string) {
  const { churn: _churn, amp: _amp, tested: _tested, ...unit } = CODE_UNITS[symbol];
  return { unit_id: `s-${symbol}`, symbol, tier: "scip", elided: false, ...unit, kind: "symbol" };
}

/** A hybrid search hit: a chunk unit, which carries no tier and no signature. */
function codeUnit(symbol: string, score: number) {
  const { signature: _signature, ...unit } = CODE_UNITS[symbol];
  return { unit_id: `u-${symbol}`, symbol, tier: "", elided: false, score, ...unit };
}

const CODE_CALLERS: Record<string, Array<[string, string, number]>> = {
  "payments.fx.convert": [
    ["payments.refunds.refund", "payments/refunds.py", 10],
    ["payments.ledger.settle_daily", "payments/ledger.py", 61],
  ],
  "payments.refunds.refund": [["payments.api.handle_refund", "payments/api.py", 9]],
};

const CODE_CALLEES: Record<string, Array<[string, string, number]>> = {
  "payments.refunds.refund": [
    ["payments.fx.convert", "payments/refunds.py", 10],
    ["payments.ledger.post", "payments/refunds.py", 11],
  ],
};

function edges(table: Record<string, Array<[string, string, number]>>, name: string, side: "from" | "to") {
  return (table[name] ?? []).map(([other, file, line]) => ({
    edge_kind: "call",
    from_symbol: side === "from" ? other : name,
    to_symbol: side === "from" ? name : other,
    from_symbol_id: "",
    to_symbol_id: "",
    path: file,
    line,
    tier: "scip",
    confidence: 1,
  }));
}

/** The assistant's answer to the refund question, as the chat route streams it. */
const ASSISTANT_ANSWER = [
  "A refund over **$250** needs a second approver in the billing console before it is issued [doc:policies/refunds.md § Approvals].",
  "",
  "- The approver must not be the person who took the original payment [doc:policies/refunds.md § Approvals].",
  "- A refund in another currency settles at the day's rate, rounded half to even [memory:payments/fx/rounding@v1].",
].join("\n");

const ASSISTANT_CITATIONS = [
  {
    kind: "doc",
    text: "[doc:policies/refunds.md § Approvals]",
    verified: true,
    reason: "",
    ref: "policies/refunds.md",
  },
  {
    kind: "memory",
    text: "[memory:payments/fx/rounding@v1]",
    verified: true,
    reason: "",
    ref: "payments/fx/rounding",
  },
];

/** The answer as server-sent chunks, its usage and checked citations, then the end. */
function assistantStream(): string {
  const pieces = ASSISTANT_ANSWER.match(/[\s\S]{1,24}/g) ?? [];
  const events: unknown[] = pieces.map((p) => ({ id: "chatcmpl-1", choices: [{ index: 0, delta: { content: p } }] }));
  events.push({ id: "chatcmpl-1", choices: [], usage: { prompt_tokens: 1800, completion_tokens: 140 } });
  // The bridge's own events once the answer ends: the text is done, then the checked
  // citations with the steps the answer's run took.
  events.push({ object: "loom.text_done", id: "chatcmpl-1", choices: [] });
  events.push({
    object: "loom.citations",
    id: "chatcmpl-1",
    choices: [],
    citations: ASSISTANT_CITATIONS,
    support: "supported",
    unknown_names: [],
    steps: [
      { op: "knowledge.search", text: "Searched documents", ok: true },
      { op: "memory.search", text: "Searched memories", ok: true },
    ],
  });
  return `${events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("")}data: [DONE]\n\n`;
}

const debugCalls: string[] = [];

// The unmocked calls each test made, printed with LOOM_DOCS_DEBUG=1.
test.afterEach(() => {
  if (process.env.LOOM_DOCS_DEBUG) console.log(debugCalls.join("\n"));
  debugCalls.length = 0;
});

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
      case "/v1/knowledge.source.list":
        return json({ sources: [], enabled: false });
      case "/v1/collection.list":
        return json({ collections: [], next_cursor: null });
      case "/v1/knowledge.list":
        return json({ rows: DOCUMENTS.map(documentRow), next_cursor: null });
      case "/v1/knowledge.search":
        return json({ results: PASSAGES, embedding_model: "text-embedding-3-small" });
      case "/v1/audit.list":
        return json({ rows: AUDIT_ROWS, next_cursor: null });
      case "/v1/code.index_status":
        return json(indexStatus());
      case "/v1/code.coverage":
        return json(coverage());
      case "/v1/code.index_errors":
        return json({ rows: [], total: 0 });
      case "/v1/code.recent_commits":
        return json({
          rows: [
            {
              commit: "4be19a0c7d2e5f13",
              subject: "The refund handler reports its status",
              h: 0.4,
              files: 2,
              pr: 488,
            },
            {
              commit: "2d71c9e0b5a3f846",
              subject: "Refunds convert to the settlement currency before posting",
              h: 6,
              files: 3,
              pr: 486,
            },
            {
              commit: "a90f4c2e7b1d3e58",
              subject: "Ledger rejects unbalanced entries per currency",
              h: 26,
              files: 1,
              pr: 479,
            },
          ].map(({ commit, subject, h, files, pr }) => ({
            commit,
            subject,
            committed_at: agoCh(h),
            ingested_at: agoCh(h - 0.1),
            files,
            pr_number: pr,
          })),
        });
      case "/v1/code.search":
        return json({
          results: [codeUnit("payments.fx.convert", 0.91), codeUnit("payments.refunds.refund", 0.74)],
          route: "hybrid",
          abstain: false,
          abstain_reason: "",
          retrieval_risk: "low",
          retrieval_confidence: 0.88,
          tokens_returned: 412,
          as_of: { commit: "4be19a0c7d2e5f13", indexed_at: agoCh(0.3), lag_commits: 0, stale: false },
        });
      case "/v1/code.symbol": {
        const name = String(body.name ?? "");
        return json({
          definitions: CODE_UNITS[name] ? [symbolUnit(name)] : [],
          ambiguous: false,
          candidates: [],
          tokens_returned: 380,
        });
      }
      case "/v1/code.callers":
        return json({
          edges: edges(CODE_CALLERS, String(body.name ?? ""), "from"),
          seed_symbols: [String(body.name ?? "")],
          epistemic: "exact",
          truncated: false,
        });
      case "/v1/code.callees":
        return json({
          edges: edges(CODE_CALLEES, String(body.name ?? ""), "to"),
          seed_symbols: [String(body.name ?? "")],
          epistemic: "exact",
          truncated: false,
        });
      case "/v1/health/ready":
        return json({ ready: true, checks: { assistant: { ok: true, required: false } } });
      case "/v1/assistant/chat/completions":
        return route.fulfill({ status: 200, contentType: "text/event-stream", body: assistantStream() });
      case "/v1/ui.vitals":
        return json({ recorded: true, route: body.route ?? "" });
      default:
        break;
    }
    debugCalls.push(`${p} ${JSON.stringify(body).slice(0, 300)}`);
    return json({ rows: [], entries: [], total: 0, today: 0, truncated: false });
  });
}

/** Let fonts, transitions and the after-paint mounts settle before the frame. */
async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(400);
  await page.mouse.move(0, 0);
}

async function capture(page: Page, name: string): Promise<void> {
  fs.mkdirSync(OUT, { recursive: true });
  await settle(page);
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

/** The page as WebP in docs/assets/screenshots/, encoded by Chromium on a blank page. */
async function captureWebp(page: Page, name: string): Promise<void> {
  fs.mkdirSync(OUT_ASSETS, { recursive: true });
  await settle(page);
  const png = (await page.screenshot()).toString("base64");
  const blank = await page.context().newPage();
  const webp = await blank.evaluate(async (data) => {
    const img = new Image();
    img.src = `data:image/png;base64,${data}`;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("no 2d canvas");
    context.drawImage(img, 0, 0);
    return canvas.toDataURL("image/webp", 0.85);
  }, png);
  await blank.close();
  // A browser without a WebP encoder answers PNG instead, which must not be saved as WebP.
  if (!webp.startsWith("data:image/webp;base64,")) throw new Error(`${name}: no WebP encoder`);
  fs.writeFileSync(path.join(OUT_ASSETS, `${name}.webp`), Buffer.from(webp.split(",", 2)[1], "base64"));
}

/** A short pause, so a clip's viewer can follow each step. */
const beat = (page: Page, ms = 900) => page.waitForTimeout(ms);

/** How a step runs: a shot goes straight to its frame, a clip types at a reading pace and
 *  pauses where a viewer needs a moment. */
interface Pace {
  clip: boolean;
  /** Milliseconds between typed characters. */
  typing: number;
}
const SHOT: Pace = { clip: false, typing: 0 };
const CLIP: Pace = { clip: true, typing: 45 };

/** The steps the shots and the clips share, each ending on what it shows. */
const steps = {
  async linkedCode(page: Page, pace = SHOT) {
    await page.goto("/memories");
    const row = page.getByText("payments/fx/rounding", { exact: true }).first();
    if (pace.clip) {
      await row.scrollIntoViewIfNeeded();
      await beat(page, 1200);
    }
    await row.click();
    await expect(page.getByRole("dialog").getByText("Linked code")).toBeVisible();
  },
  async documentSearch(page: Page) {
    await page.goto("/library");
    await expect(page.getByText("Refunds policy").first()).toBeVisible();
    await page.getByRole("button", { name: "Find a passage" }).click();
    const field = page.getByRole("textbox", { name: "Passage to find" });
    await field.click();
    await field.fill("Who approves a large refund?");
    await field.press("Enter");
    await expect(page.getByText("Approvals").first()).toBeVisible();
  },
  async codeCallers(page: Page, pace = SHOT) {
    await page.goto(`/repositories?repo=${CODE_NS}`);
    await page.getByRole("tab", { name: "Explore" }).click();
    const search = page.getByRole("textbox", { name: "Search code" });
    await search.click();
    await search.pressSequentially("where are refunds converted to USD", { delay: pace.typing });
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page.getByText("payments.fx.convert").first()).toBeVisible();
    if (pace.clip) await beat(page);
    await page.getByRole("button", { name: "payments.fx.convert", exact: true }).first().click();
    await expect(page.getByText(/^Callers \(\d+\)$/)).toBeVisible();
  },
  async assistant(page: Page, pace = SHOT) {
    await page.goto("/memories");
    const launcher = page.locator('[data-focus-key="assistant-launcher"]');
    await expect(launcher).toBeVisible();
    await launcher.click();
    const question = page.getByRole("textbox", { name: "Question" });
    await question.pressSequentially("What does a refund over $250 need?", { delay: pace.typing });
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const checked = page.getByText("Checked against what Loom holds").first();
    await expect(checked).toBeVisible();
    // The clip ends on the checked sources, below the answer's last line.
    if (pace.clip) await page.getByRole("button", { name: "Retry" }).scrollIntoViewIfNeeded();
  },
};

test.describe("docs screenshots", () => {
  test.skip(!ENABLED, "set LOOM_DOCS_SCREENSHOTS=1 to regenerate docs/images");
  test.use({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2, colorScheme: "dark" });

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

test.describe("docs screenshots at 1440 px", () => {
  test.skip(!ENABLED, "set LOOM_DOCS_SCREENSHOTS=1 to regenerate docs/assets/screenshots");
  test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "dark" });

  test("memory linked code", async ({ page }) => {
    await installDemoApi(page);
    await steps.linkedCode(page);
    await captureWebp(page, "memory-linked-code");
  });

  test("memories", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/memories");
    await expect(page.getByText("deploys/policy/windows").first()).toBeVisible();
    await captureWebp(page, "memories");
  });

  test("review queue", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/admin/review_queue");
    await expect(page.getByText("Conflict 1 of 2")).toBeVisible();
    await captureWebp(page, "review-queue");
  });

  test("audit log", async ({ page }) => {
    await installDemoApi(page);
    await page.goto("/admin/activity");
    await expect(page.getByText(/Granted writer on/).first()).toBeVisible();
    await captureWebp(page, "audit-log");
  });

  test("document search", async ({ page }) => {
    await installDemoApi(page);
    await steps.documentSearch(page);
    await captureWebp(page, "document-search");
  });

  test("code callers", async ({ page }) => {
    await installDemoApi(page);
    await steps.codeCallers(page);
    await captureWebp(page, "code-callers");
  });

  test("assistant answer", async ({ page }) => {
    await installDemoApi(page);
    await steps.assistant(page);
    await captureWebp(page, "assistant-answer");
  });
});

// The clips: the same steps, typed at a reading pace, recorded by Playwright as WebM (a
// context of their own, since `video` cannot be set per describe) and copied to a stable
// name once the page closes.
test.describe("docs clips", () => {
  test.skip(!ENABLED, "set LOOM_DOCS_SCREENSHOTS=1 to regenerate docs/assets/screenshots");
  // Smaller than the shots: a clip plays inline at page width, where 1280 px reads the same
  // and the file stays small.
  const SIZE = { width: 1280, height: 800 };

  async function record(browser: Browser, name: string, run: (page: Page) => Promise<void>): Promise<void> {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "loom-clip-"));
    try {
      const context = await browser.newContext({
        viewport: SIZE,
        deviceScaleFactor: 1,
        colorScheme: "dark",
        baseURL: test.info().project.use.baseURL,
        recordVideo: { dir, size: SIZE },
      });
      const page = await context.newPage();
      const video = page.video();
      if (!video) throw new Error("the context records no video");
      try {
        await installDemoApi(page);
        await run(page);
        await beat(page, 1600);
      } finally {
        // The recording is written out when its context closes.
        await context.close();
      }
      fs.mkdirSync(OUT_ASSETS, { recursive: true });
      await video.saveAs(path.join(OUT_ASSETS, `clip-${name}.webm`));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  test("clip memory linked code", async ({ browser }) => {
    await record(browser, "memory-linked-code", (page) => steps.linkedCode(page, CLIP));
  });

  test("clip assistant answer", async ({ browser }) => {
    await record(browser, "assistant-answer", (page) => steps.assistant(page, CLIP));
  });

  test("clip code callers", async ({ browser }) => {
    await record(browser, "code-callers", (page) => steps.codeCallers(page, CLIP));
  });
});
