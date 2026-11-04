import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Assistant page reads nothing on mount, so the route-wide axe and layout runs see only
// its starters. This spec draws every panel it has, with the server's full-length text and a
// long subject id, and checks it at desktop and phone width: no horizontal scroll, no axe
// violation.

const LONG_ID = `user-${"x".repeat(200)}`;
const OWN_USER =
  "Live rows filed under this user_id, whoever wrote them: what a token pinned to this user (read_scope own_user) reads. Promoted rows (memory.promote) are not counted.";
const OTHERS_USER =
  "Every other live row of the namespace. A member reads these too; a token pinned to this user does not. Promoted rows (memory.promote) are not counted.";

const row = (key: string) => ({
  memory_key: key,
  memory_id: `id-${key}`,
  version: 3,
  content_sha256: "ab12",
  memory_type: "semantic",
  status: "active",
  source_class: "direct",
  agent: "claude-code",
  user_id: LONG_ID,
  session_id: "-",
  actor: "human:o@example.com",
  updated_at: "2026-11-01T10:00:00",
  sensitivity: "normal",
  versions: 3,
  excerpt: "Invoices go out on Fridays, and a reminder goes to the account owner two days before.",
});

const view = (definition: string, keys: string[]) => ({
  definition,
  live_rows: keys.length,
  by_type: keys.length ? [{ name: "semantic", rows: keys.length }] : [],
  by_status: keys.length ? [{ name: "active", rows: keys.length }] : [],
  by_source_class: [],
  top_tags: keys.length ? [{ name: "billing", rows: keys.length }] : [],
  categories: [],
  first_updated_at: null,
  last_updated_at: null,
  newest: keys.map(row),
  complete: true,
});

const INSPECTION = {
  org: "dev",
  namespace: "personal-alice",
  subject: "user",
  id: LONG_ID,
  computed_at: "2026-11-01T10:00:00",
  own: view(OWN_USER, ["billing/invoices/a-rather-long-key-name-that-a-writer-chose"]),
  others: view(OTHERS_USER, ["notes/standup"]),
  activity: {
    window_seconds: 7 * 86_400,
    calls: [{ operation: "memory.search", calls: 9, succeeded: 8 }],
    first_at: "2026-10-28T10:00:00",
    last_at: "2026-11-01T10:00:00",
    gap: "",
  },
  settings: {
    guide_revision: 2,
    guide_summary: "key layout",
    categories_registered: 1,
    extraction_policy: "Keep costs and ages.\nNever store credentials.",
    extraction_policy_unavailable: "",
    extraction_instructions:
      "Per request only: memory.set_from_messages takes `extraction_instructions`; no user, agent or session setting stores them.",
  },
  skipped: [],
};

const INVENTORY = {
  scope: { org: "dev", namespace: "personal-alice", read_scope: "namespace", user_id: null, session_id: null },
  sections: [
    { plane: "memories", count: 42, latest_at: "2026-11-01T10:00:00", complete: true, reason: "" },
    { plane: "records", count: null, latest_at: null, complete: false, reason: "read failed: TimeoutError" },
    { plane: "documents", count: 3, latest_at: null, complete: true, reason: "" },
    { plane: "runs", count: 0, latest_at: null, complete: true, reason: "" },
    { plane: "integrations", count: 1, latest_at: null, complete: true, reason: "" },
  ],
  populations: [
    {
      population: "members",
      total: 2,
      top: [],
      complete: false,
      reason: "shared with everyone, a group or an agent: those members are not counted",
    },
    {
      population: "agents",
      total: 1,
      top: [{ name: "a-very-long-agent-name-from-a-client-config", rows: 40 }],
      complete: true,
      reason: "",
    },
    { population: "authors", total: 1, top: [{ name: "human:o@example.com", rows: 42 }], complete: true, reason: "" },
    {
      population: "subjects",
      total: 2,
      top: [
        { name: LONG_ID, rows: 12 },
        { name: "ada", rows: 3 },
      ],
      complete: true,
      reason: "",
    },
  ],
  capabilities: [
    { name: "llm", enabled: false },
    { name: "embedder", enabled: true },
  ],
  computed_at: "2026-11-01T10:00:00",
};

const TAG = "billing-and-invoicing-for-enterprise-accounts";
const LONG_URI = `https://example.com/${"specs/".repeat(30)}billing.pdf`;

function stats(size: number, stride: number, cut: number) {
  return {
    size,
    stride,
    chunks: 120,
    min_chars: 40,
    median_chars: size - 200,
    p90_chars: size,
    max_chars: size,
    sections: 40,
    sections_cut: cut,
    max_input_tokens: size / 4,
    over_input_limit: 0,
  };
}

const finding = (code: string, kind: string, count: number) => ({
  code,
  kind,
  severity: count ? "warning" : "info",
  rule_version: 1,
  source_op: "knowledge.inspect",
  count,
  share: count / 4,
  population: 4,
  sample_limit: 20,
  samples: count ? [LONG_URI] : [],
  samples_complete: count <= 1,
  ingestion_ids: count ? ["0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"] : [],
  without_document: 0,
  ingested: 0,
  ingested_samples: [],
  detail:
    "Sources whose job failed, or waits to retry, because no extractor could take its format: none is configured or routed for it.",
  remedy:
    "Configure an extractor for the format (LOOM_DOCUMENT_EXTRACTOR_URL, or a route in LOOM_DOCUMENT_EXTRACTORS_JSON) and upload the original again.",
  exceptions: "",
  uncertainty: "",
});

const job = {
  ingestion_id: "0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
  state: "failed",
  stage: "parse",
  attempts: 3,
  error_code: "format_unavailable",
  failure_class: "extractor_unavailable",
  queued_at: "2026-11-01T09:00:00",
  updated_at: "2026-11-01T09:01:00",
  ended_at: "2026-11-01T09:01:00",
  next_attempt_at: null,
  stalled: false,
};

const stage = { total: 3, confirmed: 1, unconfirmed: 2, source_changed: 1, outcome_known: 0 };

const KNOWLEDGE = {
  org: "dev",
  namespace: "personal-alice",
  computed_at: "2026-11-01T10:00:00",
  documents: {
    live: 2,
    deleted: 1,
    newest_ingested_at: "2026-11-01T09:01:00",
    by_format: [
      { name: "pdf", live: 1, deleted: 0 },
      { name: "application/vnd.openxmlformats-officedocument.presentationml.presentation", live: 1, deleted: 1 },
    ],
    by_source_scheme: [{ name: "https", live: 2, deleted: 1 }],
    earlier_revisions: 3,
    unnumbered: 1,
    retired_versions: 4,
  },
  chunks: {
    embedder_model: "text-embedding-3-small",
    live: 12,
    searchable: 11,
    unembedded: 1,
    by_model: [
      { model: "text-embedding-3-small", live: 11, embedded: 11 },
      { model: "a-much-older-embedding-model-name", live: 1, embedded: 0 },
    ],
  },
  decisions: {
    definition:
      "Live decision rows in this namespace whose source_resource_id cites a section of one of its documents (knowledge:<namespace>/<source_uri>#<section>).",
    all_documents: { decided: stage, proposed: stage },
  },
  listed: [
    {
      source_uri: LONG_URI,
      title: `Billing ${"specification ".repeat(12)}`,
      format: "pdf",
      source_scheme: "https",
      version: 2,
      revision: 2,
      ingestion_id: job.ingestion_id,
      ingested_at: "2026-11-01T09:01:00",
      retired_versions: 1,
      chunks: {
        live: 8,
        searchable: 7,
        unembedded: 1,
        other_model: 1,
        versions: 2,
        min_chars: 120,
        median_chars: 900,
        max_chars: 1200,
        models: ["text-embedding-3-small"],
      },
      decisions: { decided: stage, proposed: stage },
      extraction: { unit: "pages", total: 10, extracted: 9, failed: 1, lost: ["section_path"], complete: false },
      jobs: {
        source_uri: LONG_URI,
        jobs: 2,
        open_jobs: 0,
        open_ingestion_id: "",
        latest: job,
        current: job,
        replaced: false,
      },
    },
  ],
  listed_complete: false,
  findings: [finding("unembedded_chunks", "system_state", 1), finding("mixed_live_versions", "usage_heuristic", 0)],
  coverage: { rules_run: [], rules_skipped: [], complete: false },
  ingestions: {
    sources: 4,
    without_document: 1,
    replaced: 0,
    pending: 1,
    processing: 0,
    stalled: 0,
    ready: 2,
    partial: 0,
    failed: 1,
    cancelled: 0,
    oldest_open_queued_at: "2026-11-01T09:00:00",
    worker_enabled: true,
    extractor_configured: false,
    without_document_listed: [
      {
        source_uri: LONG_URI,
        jobs: 1,
        open_jobs: 0,
        open_ingestion_id: "",
        latest: job,
        current: job,
        replaced: false,
      },
    ],
  },
  extraction: { documents: 1, incomplete: 1, lost: [{ name: "section_path", live: 1, deleted: 0 }] },
  ingest_findings: [
    finding("ingest_backlog", "system_state", 1),
    finding("extractor_unavailable", "system_state", 3),
    finding("ingest_missing_access", "system_state", 1),
    finding("ingest_content_rejected", "contract_violation", 2),
    finding("extraction_incomplete", "usage_heuristic", 1),
  ],
  memory_findings: [
    {
      code: "oversized_values",
      kind: "usage_heuristic",
      severity: "warning",
      rule_version: 1,
      source_op: "namespace.audit",
      count: 2,
      share: 0.05,
      detail: "Rows over the value size a memory should hold.",
      remedy: "Move them to documents.",
    },
  ],
  skipped: ["memory_findings.episodic_blobs: not reported by namespace.audit"],
};
const TUNE = {
  org: "dev",
  namespace: "personal-alice",
  computed_at: "2026-11-01T10:00:00",
  proposals: [
    {
      id: "2ef4b21f4c7eb774",
      target: "namespace.category",
      scope: "namespace dev/personal-alice",
      title: `Register a category for the tag \`${TAG}\``,
      rationale: `12 live rows carry the tag \`${TAG}\` and no category is named after it; 4 of them are filed under another category. A registered category is what namespace.search_categories finds, and extraction offers the first 32 by name as tags for new rows.`,
      current: "",
      current_revision: null,
      proposed: TAG,
      evidence: {
        finding_codes: [],
        supporting_count: 12,
        supporting: [
          {
            memory_key: "billing/invoices/a-rather-long-key-name-that-a-writer-chose",
            memory_id: "id-billing",
            version: 3,
            content_sha256: "ab12",
            agent: "claude-code",
            user_id: "ada",
            session_id: "-",
            memory_type: "semantic",
            source_class: "direct",
            updated_at: "2026-11-01T10:00:00",
          },
        ],
        counter_count: 4,
      },
      affected_agents: ["a-very-long-agent-name-from-a-client-config"],
      affected_agents_total: 3,
      effect: "future",
      historical:
        "Registering files no row: rows already tagged join the category only through memory.assign_category, a separate action per row.",
      apply: {
        op: "namespace.register_category",
        args: { org: "dev", namespace: "personal-alice", name: TAG, description: `Rows tagged \`${TAG}\`.` },
        args_complete: true,
        required_role: "writer",
        guard:
          "None: registering a name that exists updates that category, so read namespace.list_categories again just before applying.",
      },
      limits: "A tag used for two meanings would need two categories, not one.",
    },
    {
      id: "c1",
      target: "settings.knowledge_chunking",
      scope: "every namespace of this deployment (a deployment setting)",
      title: "Cut documents into chunks of 2000 characters",
      rationale:
        "Of 30 documents of dev/personal-alice cut again at the current values, the size cuts 18 of 40 sections into more chunks than their structure makes, so a search hit returns part of a section.",
      current: "LOOM_KNOWLEDGE_CHUNK_SIZE=1000, LOOM_KNOWLEDGE_CHUNK_STRIDE=800",
      current_revision: null,
      proposed: "LOOM_KNOWLEDGE_CHUNK_SIZE=2000, LOOM_KNOWLEDGE_CHUNK_STRIDE=1600",
      evidence: {
        finding_codes: [],
        supporting_count: 12,
        supporting: [],
        counter_count: 18,
        documents: [LONG_URI],
      },
      affected_agents: [],
      affected_agents_total: 0,
      effect: "future",
      historical: "Documents already stored keep their chunks.",
      apply: {
        op: "settings.set",
        via: "admin_route",
        args: { name: "knowledge_chunk_size", value: 2000, expected: 1000 },
        args_complete: true,
        required_role: "platform_admin",
        guard: "expected: the change is refused when the value in force is no longer the drafted current one.",
      },
      apply_also: [
        {
          op: "settings.set",
          via: "admin_route",
          args: { name: "knowledge_chunk_stride", value: 1600, expected: 800 },
          args_complete: true,
          required_role: "platform_admin",
          guard: "expected: the change is refused when the value in force is no longer the drafted current one.",
        },
      ],
      preview: { before: stats(1000, 800, 18), after: stats(2000, 1600, 2) },
      limits: "The settings are the deployment's: every namespace's next writes are cut at them.",
    },
    {
      id: "r1",
      target: "settings.document_extractors_json",
      scope: "every namespace of this deployment (a deployment setting)",
      title: "Route pdf to an extractor that declares it",
      rationale: "pdf: 2 sources failed format_unavailable; no extractor is configured for it.",
      current: "{}",
      current_revision: null,
      proposed: '{"pdf": "<the URL LOOM_DOCUMENT_EXTRACTOR_URL names>"}',
      evidence: {
        finding_codes: ["extractor_unavailable"],
        supporting_count: 2,
        supporting: [],
        counter_count: 0,
        documents: [LONG_URI],
      },
      affected_agents: [],
      affected_agents_total: 0,
      effect: "future",
      historical: "Sources already ingested keep their extraction.",
      apply: {
        op: "LOOM_DOCUMENT_EXTRACTORS_JSON",
        via: "environment",
        args: { LOOM_DOCUMENT_EXTRACTORS_JSON: { pdf: "<the URL LOOM_DOCUMENT_EXTRACTOR_URL names>" } },
        args_complete: false,
        required_role: "platform_admin",
        guard: "None: an environment setting has no revision; compare the deployment's value with `current`.",
      },
      apply_also: [],
      preview: null,
      limits: "A URL is not shown, as an extractor URL can carry a credential.",
    },
  ],
  chunking: {
    cap: 200,
    population: 30,
    documents: 30,
    unread: 0,
    load_cap: 20000000,
    embedder_model: "text-embedding-3-small",
    input_token_limit: 8191,
    current: stats(1000, 800, 18),
  },
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: "",
  extraction_policy_unavailable: "",
  skipped: ["audit.telemetry_unavailable: window within the ledger lag"],
  model_used: false,
};

async function openWithEveryPanel(page: Page) {
  await installApi(page);
  // Routes added after installApi answer first.
  for (const [op, body] of [
    ["memory.inspect", INSPECTION],
    ["namespace.inventory", INVENTORY],
    ["namespace.tune", TUNE],
    ["knowledge.inspect", KNOWLEDGE],
  ] as const)
    await page.route(`**/v1/${op}`, (r) => r.fulfill({ json: body }));
  await page.goto("/assistant");
  await expect(page.getByRole("heading", { level: 1, name: "Assistant" })).toBeVisible();
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await expect(page.getByTestId("assistant-documents")).toBeVisible();
  await page.getByRole("button", { name: "User", exact: true }).click();
  await page.getByRole("button", { name: "What is in this project?" }).click();
  await expect(page.getByRole("table", { name: "What this namespace holds" })).toBeVisible();
  await page.getByLabel("User id").fill(LONG_ID);
  await page.getByLabel("User id").press("Enter");
  await expect(page.getByTestId("assistant-inspection")).toBeVisible();
  await page.getByRole("button", { name: "Configure this project" }).click();
  await expect(page.getByTestId("assistant-review")).toBeVisible();
  await page.waitForTimeout(300); // entry animations
}

// One starter panel is open at a time; each is measured in turn with the rest drawn.
const STARTER_PANELS: Array<[string, (page: Page) => Promise<void>]> = [
  [
    "What is in this project?",
    async (page) => expect(page.getByRole("table", { name: "What this namespace holds" })).toBeVisible(),
  ],
  [
    "Who are the users in this project?",
    async (page) => expect(page.getByTestId("assistant-populations")).toBeVisible(),
  ],
  ["Show relationships between facts", async (page) => expect(page.getByLabel("Entity")).toBeVisible()],
];

async function openStarter(page: Page, [name, shown]: (typeof STARTER_PANELS)[number]) {
  await page.getByRole("button", { name }).click();
  await shown(page);
  await page.waitForTimeout(300);
}

async function clipped(page: Page): Promise<string[]> {
  // Nothing runs past the box that clips or scrolls it: text cut off inside a panel
  // does not widen the page, so the page width alone would miss it.
  return page.evaluate(() => {
    const main = document.querySelector("main");
    if (!main) return ["no main"];
    const clipOf = (el: Element): Element => {
      for (let a = el.parentElement; a && a !== main; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") return a;
      }
      return main;
    };
    return [...main.querySelectorAll("*")]
      .filter((el) => el.getBoundingClientRect().width > 0)
      .filter((el) => el.getBoundingClientRect().right > clipOf(el).getBoundingClientRect().right + 1)
      .map((el) => `${el.tagName}.${el.className} ${(el.textContent ?? "").slice(0, 50)}`);
  });
}

for (const width of [1400, 375]) {
  test.describe(`${width} px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("every panel fits the page width", async ({ page }) => {
      await openWithEveryPanel(page);
      for (const starter of STARTER_PANELS) {
        await openStarter(page, starter);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow, starter[0]).toBeLessThanOrEqual(0);
        expect(await clipped(page), starter[0]).toEqual([]);
      }
    });

    test("every panel has no WCAG A/AA violations", async ({ page }) => {
      await openWithEveryPanel(page);
      for (const starter of STARTER_PANELS) {
        await openStarter(page, starter);
        expect(await violations(page), starter[0]).toEqual([]);
      }
    });
  });
}

test("Back from the graph lands on the Relationships starter", async ({ page }) => {
  await installApi(page);
  await page.goto("/assistant");
  await page.getByRole("button", { name: "Show relationships between facts" }).click();
  await page.getByLabel("Entity").fill("invoices");
  await page.getByRole("button", { name: "Open the graph" }).click();
  await expect(page).toHaveURL(/\/graph\?.*seed=invoices/);
  await page.goBack();
  await expect(page.getByRole("button", { name: "Show relationships between facts" })).toBeFocused();
});

test("a listed or cited row opens in the inspector under its whole writer identity", async ({ page }) => {
  const gets: Record<string, unknown>[] = [];
  await installApi(page);
  for (const [op, body] of [
    ["memory.inspect", INSPECTION],
    ["namespace.tune", TUNE],
  ] as const)
    await page.route(`**/v1/${op}`, (r) => r.fulfill({ json: body }));
  await page.route("**/v1/memory.get", (r) => {
    gets.push(r.request().postDataJSON() as Record<string, unknown>);
    return r.fallback();
  });
  await page.goto("/assistant");
  await page.getByLabel("User id").fill(LONG_ID);
  await page.getByLabel("User id").press("Enter");
  const key = "billing/invoices/a-rather-long-key-name-that-a-writer-chose";
  await page
    .getByTestId("assistant-inspection")
    .getByRole("button", { name: `Open memory ${key}` })
    .click();
  await expect.poll(() => gets.length).toBeGreaterThan(0);
  expect(gets.at(-1)).toMatchObject({ memory_key: key, agent: "claude-code", user_id: LONG_ID, session_id: "-" });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Configure this project" }).click();
  const before = gets.length;
  await page
    .getByTestId("assistant-review")
    .getByRole("button", { name: `Open memory ${key}` })
    .click();
  await expect.poll(() => gets.length).toBeGreaterThan(before);
  expect(gets.at(-1)).toMatchObject({ memory_key: key, agent: "claude-code", user_id: "ada", session_id: "-" });
});
