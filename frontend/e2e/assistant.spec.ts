import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { installApi } from "./fixtures";

// The Assistant page reads only its overview on load, so the route-wide axe and layout
// runs see no inspection, documents or drafts. This spec draws every panel it has, with the
// server's full-length text and a long subject id, and checks it at desktop and phone
// width: no horizontal scroll, no axe violation.

const LONG_ID = `user-${"x".repeat(200)}`;
const OWN_USER =
  "Live memories saved for this user, whoever wrote them: what a token limited to this user reads. Memories promoted for every agent of the namespace are not counted.";
const OTHERS_USER =
  "Every other live memory of the namespace. A member reads these too; a token limited to this user does not. Memories promoted for every agent of the namespace are not counted.";

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

// server/memory/app/inspect.py _DEFINITION_REFS["user"].
const view = (definition: string, keys: string[]) => ({
  definition,
  refs: ["user_id", "read_scope own_user", "memory.promote"],
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
      "Each request that extracts memories from messages may send extraction instructions; no user, agent or session setting stores them.",
    extraction_instructions_refs: ["memory.set_from_messages", "extraction_instructions"],
  },
  skipped: [],
};

const INVENTORY = {
  scope: { org: "dev", namespace: "personal-alice", read_scope: "namespace", user_id: null, session_id: null },
  sections: [
    { plane: "memories", count: 42, latest_at: "2026-11-01T10:00:00", complete: true, reason: "" },
    { plane: "records", count: null, latest_at: null, complete: false, reason: "could not be read (TimeoutError)" },
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

// The chunking draft's sample: the spec and the deck cut again at size 12000. The deck's
// chunks were written at size 6000 under the earlier embedder, whose limit they fit; cut at
// 12000 for gemini-embedding-001 (2048 tokens) two of its inputs are over, so the
// over-the-limit rule drafts the largest smaller size that fits, 6000 (9000 is still over).
const CUT_NOW = {
  size: 12000,
  stride: 9600,
  chunks: 10,
  min_chars: 120,
  median_chars: 900,
  p90_chars: 11200,
  max_chars: 12000,
  sections: 11,
  sections_cut: 1,
  max_input_tokens: 3050,
  over_input_limit: 2,
};
const CUT_PROPOSED = {
  size: 6000,
  stride: 4800,
  chunks: 12,
  min_chars: 120,
  median_chars: 1000,
  p90_chars: 6000,
  max_chars: 6000,
  sections: 11,
  sections_cut: 1,
  max_input_tokens: 1530,
  over_input_limit: 0,
};

// knowledge.inspect as the server builds it (server/knowledge/app/inspect.py), with long
// URIs, titles and format names. Two live documents, both listed: the spec, ingested and
// published partial, with an unembedded chunk; a slide deck from a source sync, its chunks
// under an older embedding model. Four job sources: the spec, a first ingest queued, one
// whose extractor is unavailable (an xlsx the default extractor does not declare) and one
// whose file was rejected.
const longUri = (name: string) => `https://example.com/${"specs/".repeat(30)}${name}`;
const DECK_URI = longUri("quarterly-review.pptx");
const QUEUED_URI = longUri("queued-first-ingest.pdf");
const NO_EXTRACTOR_URI = longUri("budget-model.xlsx");
const REJECTED_URI = longUri("corrupt-export.pdf");

const DETAIL =
  "What the check matches, as the server words it: which documents or sources, and why search does or does not serve them now.";
const REMEDY =
  "What to do about it, as the server words it: the setting to change, or the original to upload again and ingest.";
// The exact names a remedy says in words, as long as the server's longest (the rechunk command).
const REFS = [
  "loom rechunk --org <org> --namespace <namespace> --source-uri <uri> --apply",
  "LOOM_DOCUMENT_EXTRACTORS_JSON",
  "GET /v1/knowledge.download",
  "POST /v1/knowledge.upload",
  "knowledge.ingest",
];

const contract = (code: string, kind: string) => ({
  code,
  kind,
  rule_version: 1,
  source_op: "knowledge.inspect",
  sample_limit: 20,
  samples_complete: true,
  detail: DETAIL,
  remedy: REMEDY,
  refs: REFS,
  exceptions: "",
  uncertainty: "",
});

// A document check over the live documents.
const docFinding = (code: string, kind: string, samples: string[], ingested: string[] = []) => ({
  ...contract(code, kind),
  severity: samples.length ? "warning" : "info",
  count: samples.length,
  population: 2,
  share: samples.length / 2,
  samples,
  ingested: ingested.length,
  ingested_samples: ingested,
});

// A job check over the sources with a job, each sample naming its job; extraction_incomplete
// over the documents with an extraction, naming none.
const ingestFinding = (
  code: string,
  kind: string,
  population: number,
  samples: Array<[string, string]>,
  noDoc = 0,
) => ({
  ...contract(code, kind),
  severity: samples.length ? "warning" : "info",
  count: samples.length,
  population,
  share: population ? samples.length / population : 0,
  samples: samples.map(([uri]) => uri),
  ingestion_ids: samples.map(([, id]) => id).filter(Boolean),
  without_document: noDoc,
});

const job = (id: string, state: string, failure_class = "", error_code = "") => ({
  ingestion_id: id,
  state,
  stage: state === "pending" ? "" : state === "failed" ? "parse" : "",
  attempts: state === "pending" ? 0 : 1,
  error_code,
  failure_class,
  queued_at: "2026-11-01T09:00:00",
  updated_at: state === "pending" ? "2026-11-01T09:00:00" : "2026-11-01T09:01:00",
  ended_at: state === "pending" ? null : "2026-11-01T09:01:00",
  // A queued job is due at once.
  next_attempt_at: state === "pending" ? "2026-11-01T09:00:00" : null,
  stalled: false,
});

const sourceJobs = (uri: string, j: ReturnType<typeof job>) => {
  const open = j.state === "pending";
  return {
    source_uri: uri,
    jobs: 1,
    open_jobs: open ? 1 : 0,
    open_ingestion_id: open ? j.ingestion_id : "",
    latest: j,
    current: j,
    replaced: false,
  };
};

const SPEC_JOB = job("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", "partial");
const QUEUED_JOB = job("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c", "pending");
const NO_EXTRACTOR_JOB = job(
  "0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5d",
  "failed",
  "extractor_unavailable",
  "format_unavailable",
);
const REJECTED_JOB = job("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5e", "failed", "bad_content", "corrupt");

const stage = { total: 3, confirmed: 1, unconfirmed: 2, source_changed: 1, outcome_known: 0 };
const none = { total: 0, confirmed: 0, unconfirmed: 0, source_changed: 0, outcome_known: 0 };
const DECK_FORMAT = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

const KNOWLEDGE = {
  org: "dev",
  namespace: "personal-alice",
  computed_at: "2026-11-01T10:00:00",
  documents: {
    live: 2,
    deleted: 1,
    newest_ingested_at: "2026-11-01T09:01:00",
    // Most live first, then most deleted.
    by_format: [
      { name: DECK_FORMAT, live: 1, deleted: 1 },
      { name: "pdf", live: 1, deleted: 0 },
    ],
    by_source_scheme: [{ name: "https", live: 2, deleted: 1 }],
    // The spec's first revision, and two of the deleted deck's; the live deck is unnumbered.
    // Retired: the spec's first version and the deleted deck's three.
    earlier_revisions: 3,
    unnumbered: 1,
    retired_versions: 4,
  },
  chunks: {
    embedder_model: "gemini-embedding-001",
    live: 12,
    searchable: 7,
    unembedded: 1,
    by_model: [
      { model: "gemini-embedding-001", live: 8, embedded: 7 },
      { model: "text-embedding-3-small", live: 4, embedded: 4 },
    ],
  },
  decisions: {
    definition:
      "Live decisions in this namespace that cite a section of one of its documents, as decision extraction and decision recording write them, within what your token reads, by stage: decided, or proposed. Decisions written into another namespace (a repository's) are not counted.",
    refs: [
      "source_resource_id",
      "knowledge:<namespace>/<source_uri>#<section>",
      "loom decisions extract",
      "decision.record",
      "decision:proposed",
    ],
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
      ingestion_id: SPEC_JOB.ingestion_id,
      ingested_at: "2026-11-01T09:01:00",
      retired_versions: 1,
      chunks: {
        live: 8,
        searchable: 7,
        unembedded: 1,
        other_model: 0,
        versions: 1,
        min_chars: 120,
        median_chars: 900,
        max_chars: 1200,
        models: ["gemini-embedding-001"],
      },
      decisions: { decided: stage, proposed: stage },
      extraction: { unit: "pages", total: 10, extracted: 9, failed: 1, lost: ["section_path"], complete: false },
      jobs: sourceJobs(LONG_URI, SPEC_JOB),
    },
    {
      source_uri: DECK_URI,
      title: `Quarterly ${"review ".repeat(12)}`,
      format: DECK_FORMAT,
      source_scheme: "https",
      version: 1,
      revision: 0,
      ingestion_id: "",
      ingested_at: "2026-10-30T09:00:00",
      // Never rewritten: the retired versions are the deleted deck's.
      retired_versions: 0,
      // Written at size 6000 under the earlier embedder.
      chunks: {
        live: 4,
        searchable: 0,
        unembedded: 0,
        other_model: 4,
        versions: 0,
        min_chars: 2000,
        median_chars: 5000,
        max_chars: 6000,
        models: ["text-embedding-3-small"],
      },
      decisions: { decided: none, proposed: none },
      extraction: null,
      jobs: null,
    },
  ],
  listed_complete: true,
  findings: [
    docFinding("documents_without_chunks", "system_state", []),
    docFinding("unembedded_chunks", "system_state", [LONG_URI], [LONG_URI]),
    docFinding("chunks_under_other_model", "system_state", [DECK_URI]),
    docFinding("mixed_live_versions", "usage_heuristic", []),
  ],
  coverage: {
    rules_run: [
      "documents_without_chunks",
      "unembedded_chunks",
      "chunks_under_other_model",
      "mixed_live_versions",
      "ingest_backlog",
      "extractor_unavailable",
      "ingest_missing_access",
      "ingest_content_rejected",
      "ingest_failed_on_server",
      "extraction_incomplete",
    ],
    rules_skipped: [],
    complete: true,
  },
  ingestions: {
    sources: 4,
    without_document: 3,
    replaced: 0,
    pending: 1,
    processing: 0,
    stalled: 0,
    ready: 0,
    partial: 1,
    failed: 2,
    cancelled: 0,
    oldest_open_queued_at: QUEUED_JOB.queued_at,
    worker_enabled: true,
    // LOOM_DOCUMENT_EXTRACTOR_URL, and a pptx route.
    extractor_configured: true,
    // Newest queued first, then by URI (store.sources_without_document): queued together.
    without_document_listed: [
      sourceJobs(NO_EXTRACTOR_URI, NO_EXTRACTOR_JOB),
      sourceJobs(REJECTED_URI, REJECTED_JOB),
      sourceJobs(QUEUED_URI, QUEUED_JOB),
    ].sort((a, b) => (a.source_uri < b.source_uri ? -1 : 1)),
  },
  extraction: { documents: 1, incomplete: 1, lost: [{ name: "section_path", live: 1, deleted: 0 }] },
  ingest_findings: [
    ingestFinding("ingest_backlog", "system_state", 4, [[QUEUED_URI, QUEUED_JOB.ingestion_id]], 1),
    ingestFinding("extractor_unavailable", "system_state", 4, [[NO_EXTRACTOR_URI, NO_EXTRACTOR_JOB.ingestion_id]], 1),
    ingestFinding("ingest_missing_access", "system_state", 4, []),
    ingestFinding("ingest_content_rejected", "contract_violation", 4, [[REJECTED_URI, REJECTED_JOB.ingestion_id]], 1),
    ingestFinding("ingest_failed_on_server", "system_state", 4, []),
    ingestFinding("extraction_incomplete", "usage_heuristic", 1, [[LONG_URI, ""]]),
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
      detail:
        "Searchable rows longer than the memory value advisory (30000 chars): a document, not a memory, and past the embedder's cap it is never embedded at all.",
      remedy:
        "Store the document with `knowledge.upsert` and write a one-paragraph memory row under the same key that points at it. See docs/guides/investigation-agent-memory.md.",
    },
  ],
  skipped: ["memory_findings.episodic_blobs: not reported by the namespace audit"],
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
      rationale: `12 live memories carry the tag \`${TAG}\` and no category is named after it; 4 of them are filed under another category. A registered category is what a category search finds, and extraction offers the first 32 by name as tags for new memories.`,
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
        documents: [],
      },
      affected_agents: ["a-very-long-agent-name-from-a-client-config"],
      affected_agents_total: 3,
      effect: "future",
      historical:
        "Registering files no memory: memories already tagged join the category only when each is assigned to it, a separate action per memory.",
      apply: {
        op: "namespace.register_category",
        args: { org: "dev", namespace: "personal-alice", name: TAG, description: `Memories tagged \`${TAG}\`.` },
        args_complete: true,
        required_role: "writer",
        via: "op",
        guard:
          "None: registering a name that exists updates that category, so list the categories again just before applying.",
      },
      apply_also: [],
      preview: null,
      limits: "A tag used for two meanings would need two categories, not one.",
      refs: [
        "memory.set_from_messages",
        "namespace.search_categories",
        "memory.assign_category",
        "namespace.list_categories",
      ],
    },
    {
      id: "9c0d3a71e5b24f68",
      target: "settings.knowledge_chunking",
      scope: "every namespace of this deployment (a deployment setting)",
      title: "Cut documents into chunks of 6000 characters",
      rationale:
        "Of 2 documents of dev/personal-alice cut again at the current values, 2 of 10 sampled chunks have an embedding input over gemini-embedding-001's 2048-token limit (the largest is 3050), which the provider refuses, so a write cut like them fails. At size 6000 and stride 4800 the same sample makes 12 chunks (now 10), cuts 1 of 11 sections (now 1) and its largest embedding input is 1530 tokens (now 3050). The stride keeps its ratio to the size.",
      current: "knowledge_chunk_size=12000, knowledge_chunk_stride=9600",
      current_revision: null,
      proposed: "knowledge_chunk_size=6000, knowledge_chunk_stride=4800",
      evidence: {
        finding_codes: [],
        supporting_count: 1,
        supporting: [],
        counter_count: 1,
        documents: [DECK_URI],
      },
      affected_agents: ["a-very-long-agent-name-from-a-client-config"],
      affected_agents_total: 1,
      effect: "future",
      historical:
        "Documents already stored keep their chunks. A text document is cut again by the rechunk command; an ingested one only by downloading its original, uploading it again and ingesting it (the rechunk command would turn it into a text document). Each is a separate action with its own approval.",
      // server/knowledge/app/tune.py _CHUNKS_REFS.
      refs: [
        "loom rechunk --org <org> --namespace <namespace> --apply",
        "GET /v1/knowledge.download",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
        "settings.set knowledge_chunk_size",
        "settings.set knowledge_chunk_stride",
        "expected",
      ],
      apply: {
        op: "settings.set",
        via: "admin_route",
        args: { name: "knowledge_chunk_size", value: 6000, expected: 12000 },
        args_complete: true,
        required_role: "platform_admin",
        guard: "The value it was drafted against: the change is refused when the value in force is no longer that one.",
      },
      apply_also: [
        {
          op: "settings.set",
          via: "admin_route",
          args: { name: "knowledge_chunk_stride", value: 4800, expected: 9600 },
          args_complete: true,
          required_role: "platform_admin",
          guard:
            "The value it was drafted against: the change is refused when the value in force is no longer that one.",
        },
      ],
      preview: { before: CUT_NOW, after: CUT_PROPOSED },
      limits:
        "The settings are the deployment's: every namespace's next writes are cut at them, and the evidence is this namespace's sample. A longer chunk keeps more of a section in one vector and a shorter one less; what either does to search quality is not measured here, which a paired search benchmark shows. Until both settings are set, a write uses a stride of at most the size.",
    },
    {
      id: "4f1e8b2c7a903d56",
      target: "settings.document_extractors_json",
      scope: "every namespace of this deployment (a deployment setting)",
      title: "Route xlsx to an extractor that declares it",
      rationale:
        'xlsx: 1 source failed because no extractor took the format; it goes to LOOM_DOCUMENT_EXTRACTOR_URL, which does not declare it; the service LOOM_DOCUMENT_EXTRACTORS_JSON["pptx"] names declares xlsx. Counts are dev/personal-alice\'s.',
      current: '{"pptx": "<as set>"}',
      current_revision: null,
      proposed: '{"pptx": "<as set>", "xlsx": "<the URL LOOM_DOCUMENT_EXTRACTORS_JSON[\\"pptx\\"] names>"}',
      evidence: {
        finding_codes: ["extractor_unavailable"],
        supporting_count: 1,
        supporting: [],
        // No xlsx document was extracted whole.
        counter_count: 0,
        documents: [NO_EXTRACTOR_URI],
      },
      affected_agents: ["a-very-long-agent-name-from-a-client-config"],
      affected_agents_total: 1,
      effect: "future",
      historical:
        "Sources already ingested keep their extraction, and a failed job does not run again: each is extracted again only by uploading its original again and ingesting it, a separate action with its own approval.",
      // server/knowledge/app/tune.py: format_unavailable (a source failed), then _ROUTES_REFS.
      refs: [
        "format_unavailable",
        "LOOM_DOCUMENT_EXTRACTORS_JSON",
        "current",
        "POST /v1/knowledge.upload",
        "knowledge.ingest",
        "knowledge.inspect",
      ],
      apply: {
        op: "LOOM_DOCUMENT_EXTRACTORS_JSON",
        via: "environment",
        args: {
          LOOM_DOCUMENT_EXTRACTORS_JSON: {
            pptx: "<as set>",
            xlsx: '<the URL LOOM_DOCUMENT_EXTRACTORS_JSON["pptx"] names>',
          },
        },
        args_complete: false,
        required_role: "platform_admin",
        guard:
          "None: an environment setting has no revision; compare the deployment's value with the current one shown here before setting it. It is in force once each server and worker restarts.",
      },
      apply_also: [],
      preview: null,
      limits:
        "A URL is not shown, as an extractor URL can carry a credential: each placeholder names the setting whose URL it is, and `<as set>` keeps a route as the deployment has it. A service's capabilities name the formats it takes, not what it keeps: ingest one listed source through the new route and inspect the documents' coverage before relying on it. A format's own route wins over a media-type route (`application/pdf`), so the drafted route also takes the sources a media-type route sends elsewhere now.",
    },
  ],
  // namespace_tune_document_sample (default 50) and MAX_SAMPLE_CHARS (8 Mi).
  chunking: {
    cap: 50,
    population: 2,
    documents: 2,
    unread: 0,
    load_cap: 8 * 1024 * 1024,
    embedder_model: "gemini-embedding-001",
    input_token_limit: 2048,
    current: CUT_NOW,
  },
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: "",
  extraction_policy_unavailable: "",
  skipped: [
    "audit.telemetry_unavailable: window within the ledger lag",
    "decision_extraction: no instruction is drafted: decision extraction and a source sync's decision mining build their prompt from fixed text, and no request, namespace or org setting carries instructions for them",
  ],
  model_used: false,
};

async function installAssistant(page: Page) {
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
  await expect(page.getByTestId("assistant-populations")).toBeVisible();
}

async function openWithEveryPanel(page: Page) {
  await installAssistant(page);
  await page.getByRole("button", { name: "Inspect documents" }).click();
  await expect(page.getByTestId("assistant-documents")).toBeVisible();
  // The longest user id, picked from the overview.
  await page.getByTestId("pick-user").first().click();
  await expect(page.getByTestId("assistant-inspection")).toBeVisible();
  await page.getByRole("button", { name: "Draft improvements" }).click();
  await expect(page.getByTestId("assistant-review")).toBeVisible();
  // Exact names on every tab are part of what must fit and pass the checks.
  await expect(page.getByTestId("assistant-review").getByTestId("exact-names").first()).toBeVisible();
  await expect(page.getByTestId("assistant-documents").getByTestId("exact-names").first()).toBeVisible();
  await expect(page.getByTestId("assistant-inspection").getByTestId("exact-names").first()).toBeVisible();
  await page.waitForTimeout(300); // entry animations
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
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow).toBeLessThanOrEqual(0);
      expect(await clipped(page)).toEqual([]);
    });

    test("every panel has no WCAG A/AA violations", async ({ page }) => {
      await openWithEveryPanel(page);
      expect(await violations(page)).toEqual([]);
    });
  });
}

test("a pick is named by its aria-label, shows a cut id whole in its tip, and focuses the answer", async ({ page }) => {
  await installAssistant(page);
  await expect(page.getByRole("button", { name: "ada 3 memories", exact: true })).toBeVisible();
  const longest = page.getByTestId("pick-user").first();
  await longest.hover();
  await expect(page.getByRole("tooltip")).toContainText(LONG_ID);
  await longest.click();
  await expect(page.getByTestId("assistant-inspection")).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Memories", exact: true })).toBeFocused();
});

test("blocked picks with a long id fit the phone width", async ({ page }) => {
  // A token pinned to one user: the overview lists its user, and every pick is blocked.
  await page.setViewportSize({ width: 375, height: 900 });
  await installApi(page);
  const pinned = { ...INVENTORY, scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: LONG_ID } };
  await page.route("**/v1/namespace.inventory", (r) => r.fulfill({ json: pinned }));
  await page.goto("/assistant");
  const pick = page.getByTestId("pick-user").first();
  await expect(pick).toHaveAttribute("aria-disabled", "true");
  // The reason describes it; the tip adds the whole id under the reason.
  await expect(pick).toHaveAccessibleDescription(/limited to one user or session/);
  await pick.hover();
  await expect(page.getByRole("tooltip")).toContainText(LONG_ID);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  expect(await clipped(page)).toEqual([]);
});

test("Back from the graph lands on the button that opened it", async ({ page }) => {
  await installApi(page);
  await page.goto("/assistant");
  await page.getByLabel("Start from (optional)").fill("invoices");
  await page.getByRole("button", { name: "Open the graph" }).click();
  await expect(page).toHaveURL(/\/graph\?.*seed=invoices/);
  await page.goBack();
  await expect(page.getByRole("button", { name: "Open the graph" })).toBeFocused();
});

test("the graph opens with no entity typed", async ({ page }) => {
  await installApi(page);
  await page.goto("/assistant");
  await page.getByRole("button", { name: "Open the graph" }).click();
  await expect(page).toHaveURL(/\/graph/);
  expect(page.url()).not.toContain("seed=");
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
  await page.getByRole("button", { name: "Draft improvements" }).click();
  const before = gets.length;
  await page
    .getByTestId("assistant-review")
    .getByRole("button", { name: `Open memory ${key}` })
    .click();
  await expect.poll(() => gets.length).toBeGreaterThan(before);
  expect(gets.at(-1)).toMatchObject({ memory_key: key, agent: "claude-code", user_id: "ada", session_id: "-" });
});
