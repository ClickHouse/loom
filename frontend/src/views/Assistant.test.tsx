import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DocumentFinding,
  FindingKind,
  IngestFinding,
  IngestJob,
  InspectRow,
  KnowledgeInspection,
  InspectView,
  MemoryInspection,
  MyNamespaceEntry,
  NamespaceInventory,
  NamespaceTune,
  TuneProposal,
  WhoAmI,
} from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { Assistant } from "./Assistant";

// A row version is a nanosecond stamp (server/memory/store/rows.py), past what a JavaScript
// number holds exactly: the page never prints it.
const NS_VERSION = Number("1762156800123456789");

// The Assistant page reads only on a click, through three read-only ops, and never
// applies a draft: it renders what namespace.inventory, memory.inspect and namespace.tune
// answer, with what they could not read said so. The fixtures are the shapes the server
// sends.

const INVENTORY: NamespaceInventory = {
  scope: { org: "dev", namespace: "team", read_scope: "namespace", user_id: null, session_id: null },
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
    { population: "agents", total: 1, top: [{ name: "claude-code", rows: 40 }], complete: true, reason: "" },
    { population: "authors", total: 1, top: [{ name: "human:o@example.com", rows: 42 }], complete: true, reason: "" },
    { population: "subjects", total: 1, top: [{ name: "ada", rows: 12 }], complete: true, reason: "" },
  ],
  capabilities: [
    { name: "llm", enabled: false },
    { name: "embedder", enabled: true },
  ],
  computed_at: "2026-11-01T10:00:00",
};

const ROW: InspectRow = {
  memory_key: "billing/invoices",
  memory_id: "id-billing",
  version: NS_VERSION,
  content_sha256: "ab12",
  memory_type: "semantic",
  status: "active",
  source_class: "direct",
  agent: "claude-code",
  user_id: "ada",
  session_id: "-",
  actor: "human:o@example.com",
  updated_at: "2026-11-01T10:00:00",
  sensitivity: "normal",
  versions: 3,
  excerpt: "Invoices go out on Fridays",
};

// server/memory/app/inspect.py _DEFINITIONS["user"].
const OWN_USER =
  "Live rows filed under this user_id, whoever wrote them: what a token pinned to this user (read_scope own_user) reads. Promoted rows (memory.promote) are not counted.";
const OTHERS_USER =
  "Every other live row of the namespace. A member reads these too; a token pinned to this user does not. Promoted rows (memory.promote) are not counted.";

// live_rows is the sum of the same count rows by_type and by_status come from.
const view = (definition: string, rows: InspectRow[], liveRows = rows.length): InspectView => ({
  definition,
  live_rows: liveRows,
  by_type: liveRows ? [{ name: "semantic", rows: liveRows }] : [],
  by_status: liveRows ? [{ name: "active", rows: liveRows }] : [],
  by_source_class: [],
  top_tags: [],
  categories: [],
  first_updated_at: null,
  last_updated_at: null,
  newest: rows,
  complete: true,
});

const inspection = (subject: MemoryInspection["subject"], id: string): MemoryInspection => ({
  org: "dev",
  namespace: "team",
  subject,
  id,
  computed_at: "2026-11-01T10:00:00",
  own: view(OWN_USER, [ROW]),
  others: view(OTHERS_USER, []),
  activity: { window_seconds: 7 * 86_400, calls: [], first_at: null, last_at: null, gap: "" },
  settings: {
    guide_revision: 2,
    guide_summary: "key layout",
    categories_registered: 1,
    extraction_policy: null,
    extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
    extraction_instructions:
      "Per request only: memory.set_from_messages takes `extraction_instructions`; no user, agent or session setting stores them.",
  },
  skipped: [],
});

const CITED = {
  memory_key: "billing/invoices",
  memory_id: "id-billing",
  version: NS_VERSION,
  content_sha256: "ab12",
  agent: "claude-code",
  user_id: "ada",
  session_id: "-",
  memory_type: "semantic",
  source_class: "direct",
  updated_at: "2026-11-01T10:00:00",
};

// A category draft cites the tagged rows and counts those another category holds.
const CATEGORY: TuneProposal = {
  id: "2ef4b21f4c7eb774",
  target: "namespace.category",
  scope: "namespace dev/team",
  title: "Register a category for the tag `billing`",
  rationale:
    "12 live rows carry the tag `billing` and no category is named after it; 4 of them are filed under another category. A registered category is what namespace.search_categories finds, and extraction offers the first 32 by name as tags for new rows.",
  current: "",
  current_revision: null,
  proposed: "billing",
  evidence: { finding_codes: [], supporting_count: 12, supporting: [CITED], counter_count: 4 },
  affected_agents: ["claude-code"],
  affected_agents_total: 3,
  effect: "future",
  historical:
    "Registering files no row: rows already tagged join the category only through memory.assign_category, a separate action per row.",
  apply: {
    op: "namespace.register_category",
    args: { org: "dev", namespace: "team", name: "billing", description: "Rows tagged `billing`." },
    args_complete: true,
    required_role: "writer",
    guard:
      "None: registering a name that exists updates that category, so read namespace.list_categories again just before applying.",
  },
  limits: "A tag used for two meanings would need two categories, not one.",
};

// _guide_lines appends the prefixes an existing guide leaves out.
const GUIDE_CURRENT = "# team\n\n## Key layout\n\n- `notes/…`: 9 rows\n";
const GUIDE_PROPOSED = `${GUIDE_CURRENT}\n## Key layout\n\n- \`billing/…\`: 12 rows\n`;

// A guide draft cites no row and counts none; here the writers could not be read.
const GUIDE: TuneProposal = {
  id: "12b965a8ed32f1da",
  target: "namespace.guide",
  scope: "namespace dev/team",
  title: "Name the key prefixes the guide leaves out",
  rationale:
    "1 key prefixes holding rows are not named in the guide. A writer reads the guide before its first write, so it names the layout new rows should follow; the counts are today's live rows.",
  current: GUIDE_CURRENT,
  current_revision: 2,
  proposed: GUIDE_PROPOSED,
  evidence: { finding_codes: [], supporting_count: 12, supporting: [], counter_count: null },
  affected_agents: [],
  affected_agents_total: null,
  effect: "future",
  historical: "A guide changes no stored row; it shapes the writes that read it.",
  apply: {
    op: "namespace.guide_set",
    args: {
      org: "dev",
      namespace: "team",
      content: GUIDE_PROPOSED,
      expected_revision: 2,
      note: "Drafted by namespace.tune from the key layout and tags.",
    },
    args_complete: true,
    required_role: "writer",
    guard:
      "expected_revision: a guide written since the draft refuses the write and answers with its current text to merge into.",
  },
  limits: "The draft lists what the rows show, not what they mean; a person writes what each prefix and tag is for.",
};

// The extraction draft answers audit findings and reaches the agents extraction wrote for.
const INSTRUCTIONS: TuneProposal = {
  id: "67a699bd202bbfee",
  target: "extraction_instructions.request",
  scope: "each memory.set_from_messages request into dev/team that sends them",
  title: "Send extraction instructions for the shapes the audit found",
  rationale:
    "The audit flagged episodic_blobs: 5 of 30 rows (counts over every writer). Extraction wrote 30 of the live rows here; 1 of the audit's cited rows came from it. No user, agent or session setting stores instructions, so a writer sends them per request.",
  current: "",
  current_revision: null,
  proposed: "- Keep an episode to a short summary; a run's full output belongs in context.offload.",
  evidence: { finding_codes: ["episodic_blobs"], supporting_count: 5, supporting: [CITED], counter_count: 25 },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
  effect: "future",
  historical:
    "Rows already stored keep their shape. Reworking them is the loom-optimize procedure (memory.move per row, keeping a redirect), a separate action with its own approval.",
  apply: {
    op: "memory.set_from_messages",
    args: {
      extraction_instructions: "- Keep an episode to a short summary; a run's full output belongs in context.offload.",
    },
    args_complete: false,
    required_role: "writer",
    guard: "None needed: the instructions apply only to the request that carries them.",
  },
  limits:
    "Each writer has to send them; a request without them extracts as before. The flagged counts include rows written by hand (memory.set), which never pass through extraction; a row counts as extraction's by its source trace. They are not drafted as the operator extraction policy: a policy overrides every other instruction and turns off the recovery passes that keep preferences, costs, ages and corrections.",
};

const TUNE: NamespaceTune = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-11-01T10:00:00",
  proposals: [CATEGORY, GUIDE, INSTRUCTIONS],
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: null,
  extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
  skipped: ["audit.telemetry_unavailable: window within the ledger lag"],
  model_used: false,
};

// knowledge.inspect, as server/knowledge/app/inspect.py builds it. The base: two live
// documents, one ingested (its extraction lost a unit, so it published partial) and one a
// text write with an unembedded chunk; one job source; every check run, a clean one sent as
// an `info` finding with count 0. Each derived state below changes every field the server
// changes for it.
const SPEC = "https://example.com/spec.pdf";
const NOTES = "https://example.com/notes.md";

const DOC_CHECKS: Array<[string, FindingKind]> = [
  ["documents_without_chunks", "system_state"],
  ["unembedded_chunks", "system_state"],
  ["chunks_under_other_model", "system_state"],
  ["mixed_live_versions", "usage_heuristic"],
];

const INGEST_CODES: Array<[string, IngestFinding["kind"], string]> = [
  ["ingest_backlog", "system_state", "Backlog"],
  ["extractor_unavailable", "system_state", "Extractor unavailable"],
  ["ingest_missing_access", "system_state", "Missing access"],
  ["ingest_content_rejected", "contract_violation", "Content rejected"],
  ["ingest_failed_on_server", "system_state", "Failed on the server's side"],
  ["extraction_incomplete", "usage_heuristic", "Extraction incomplete"],
];
// The five job checks, skipped together when the job read fails (store.INGEST_CHECKS).
const JOB_CODES = INGEST_CODES.slice(0, 5).map(([c]) => c);
const ALL_CHECKS = [...DOC_CHECKS.map(([c]) => c), ...INGEST_CODES.map(([c]) => c)];

const share = (count: number, population: number) => (population ? Math.min(count / population, 1) : 0);

/** A document check: its population is the live documents. */
const docFinding = (code: string, kind: FindingKind, population: number, samples: string[] = []): DocumentFinding => ({
  code,
  kind,
  severity: samples.length ? "warning" : "info",
  rule_version: 1,
  source_op: "knowledge.inspect",
  count: samples.length,
  share: share(samples.length, population),
  population,
  sample_limit: 20,
  samples,
  samples_complete: true,
  ingested: 0,
  ingested_samples: [],
  detail: `detail of ${code}`,
  remedy: `remedy of ${code}`,
  exceptions: "",
  uncertainty: "",
});

/** An ingest check: a job check's population is the sources with a job, each sample naming
 *  its job; extraction_incomplete's is the documents with an extraction, and names none. */
const ingestFinding = (
  code: string,
  kind: IngestFinding["kind"],
  population: number,
  samples: Array<[string, string]> = [],
  withoutDocument = 0,
): IngestFinding => ({
  code,
  kind,
  severity: samples.length ? "warning" : "info",
  rule_version: 1,
  source_op: "knowledge.inspect",
  count: samples.length,
  share: share(samples.length, population),
  population,
  sample_limit: 20,
  samples: samples.map(([uri]) => uri),
  samples_complete: true,
  ingestion_ids: samples.map(([, id]) => id).filter(Boolean),
  without_document: withoutDocument,
  detail: `detail of ${code}`,
  remedy: `remedy of ${code}`,
  exceptions: "",
  uncertainty: "",
});

/** Every ingest check clean over `sources` job sources and `extracted` extractions. */
const cleanIngest = (sources: number, extracted: number) =>
  INGEST_CODES.map(([c, k]) => ingestFinding(c, k, c === "extraction_incomplete" ? extracted : sources));

const stage = (total: number) => ({
  total,
  confirmed: total ? 1 : 0,
  unconfirmed: total ? total - 1 : 0,
  source_changed: 0,
  outcome_known: 0,
});

const JOB: IngestJob = {
  ingestion_id: "ing-1",
  state: "partial",
  stage: "",
  attempts: 1,
  error_code: "",
  failure_class: "",
  queued_at: "2026-11-01T09:00:00",
  updated_at: "2026-11-01T09:01:00",
  ended_at: "2026-11-01T09:01:00",
  next_attempt_at: null,
  stalled: false,
};

const sourceJobs = (uri: string, job: IngestJob) => {
  const open = job.state === "pending" || job.state === "processing";
  return {
    source_uri: uri,
    jobs: 1,
    open_jobs: open ? 1 : 0,
    open_ingestion_id: open ? job.ingestion_id : "",
    latest: job,
    current: job,
    replaced: false,
  };
};

const NO_JOBS: KnowledgeInspection["ingestions"] = {
  sources: 0,
  without_document: 0,
  replaced: 0,
  pending: 0,
  processing: 0,
  stalled: 0,
  ready: 0,
  partial: 0,
  failed: 0,
  cancelled: 0,
  oldest_open_queued_at: null,
  worker_enabled: true,
  extractor_configured: true,
  without_document_listed: [],
};

const KNOWLEDGE: KnowledgeInspection = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-11-01T10:00:00",
  documents: {
    live: 2,
    deleted: 1,
    newest_ingested_at: "2026-11-01T09:01:00",
    // Most live first, then most deleted (inspect.py _counts).
    by_format: [
      { name: "markdown", live: 1, deleted: 1 },
      { name: "pdf", live: 1, deleted: 0 },
    ],
    by_source_scheme: [{ name: "https", live: 2, deleted: 1 }],
    // The spec's first revision, and two of the deleted document's.
    earlier_revisions: 3,
    unnumbered: 0,
    retired_versions: 4,
  },
  chunks: {
    embedder_model: "text-embedding-3-small",
    live: 12,
    searchable: 11,
    unembedded: 1,
    by_model: [{ model: "text-embedding-3-small", live: 12, embedded: 11 }],
  },
  decisions: {
    definition: "Live decision rows citing a document.",
    all_documents: { decided: stage(3), proposed: stage(0) },
  },
  listed: [
    {
      source_uri: SPEC,
      title: "Billing spec",
      format: "pdf",
      source_scheme: "https",
      version: 2,
      revision: 2,
      ingestion_id: "ing-1",
      ingested_at: "2026-11-01T09:01:00",
      retired_versions: 1,
      chunks: {
        live: 8,
        searchable: 8,
        unembedded: 0,
        other_model: 0,
        versions: 1,
        min_chars: 120,
        median_chars: 900,
        max_chars: 1200,
        models: ["text-embedding-3-small"],
      },
      decisions: { decided: stage(3), proposed: stage(0) },
      extraction: { unit: "pages", total: 10, extracted: 9, failed: 1, lost: ["section_path"], complete: false },
      jobs: sourceJobs(SPEC, JOB),
    },
    {
      source_uri: NOTES,
      title: "Notes",
      format: "markdown",
      source_scheme: "https",
      version: 1,
      revision: 1,
      ingestion_id: "",
      ingested_at: "2026-10-30T09:00:00",
      retired_versions: 0,
      chunks: {
        live: 4,
        searchable: 3,
        unembedded: 1,
        other_model: 0,
        versions: 1,
        min_chars: 80,
        median_chars: 300,
        max_chars: 600,
        models: ["text-embedding-3-small"],
      },
      decisions: { decided: stage(0), proposed: stage(0) },
      extraction: null,
      jobs: null,
    },
  ],
  listed_complete: true,
  findings: DOC_CHECKS.map(([c, k]) => docFinding(c, k, 2, c === "unembedded_chunks" ? [NOTES] : [])),
  coverage: { rules_run: ALL_CHECKS, rules_skipped: [], complete: true },
  ingestions: { ...NO_JOBS, sources: 1, partial: 1 },
  extraction: { documents: 1, incomplete: 1, lost: [{ name: "section_path", live: 1, deleted: 0 }] },
  ingest_findings: INGEST_CODES.map(([c, k]) =>
    c === "extraction_incomplete" ? ingestFinding(c, k, 1, [[SPEC, ""]]) : ingestFinding(c, k, 1),
  ),
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
  skipped: [],
};

/** No document and no job: every group read is empty, so the totals are 0 and the newest
 *  write null; each check runs over a population of 0. */
const emptyNamespace = (): KnowledgeInspection => ({
  ...KNOWLEDGE,
  documents: {
    live: 0,
    deleted: 0,
    newest_ingested_at: null,
    by_format: [],
    by_source_scheme: [],
    earlier_revisions: 0,
    unnumbered: 0,
    retired_versions: 0,
  },
  chunks: { ...KNOWLEDGE.chunks, live: 0, searchable: 0, unembedded: 0, by_model: [] },
  decisions: { ...KNOWLEDGE.decisions, all_documents: { decided: stage(0), proposed: stage(0) } },
  listed: [],
  listed_complete: true,
  findings: DOC_CHECKS.map(([c, k]) => docFinding(c, k, 0)),
  ingestions: NO_JOBS,
  extraction: { documents: 0, incomplete: 0, lost: [] },
  ingest_findings: cleanIngest(0, 0),
  memory_findings: [],
});

/** The documents read failed: its totals, groups and newest write are null or empty, and
 *  the listing cannot be complete. */
const documentsUnread = (r: KnowledgeInspection): KnowledgeInspection => ({
  ...r,
  documents: {
    ...r.documents,
    live: null,
    deleted: null,
    newest_ingested_at: null,
    by_format: [],
    by_source_scheme: [],
    earlier_revisions: null,
    unnumbered: null,
  },
  listed_complete: false,
  skipped: [...r.skipped, "documents: TimeoutError"],
});

/** The job read failed: every job count is null, nothing is listed, and the five job checks
 *  are skipped (extraction_incomplete reads the coverage and still runs). */
const jobsUnread = (r: KnowledgeInspection): KnowledgeInspection => ({
  ...r,
  ingestions: {
    sources: null,
    without_document: null,
    replaced: null,
    pending: null,
    processing: null,
    stalled: null,
    ready: null,
    partial: null,
    failed: null,
    cancelled: null,
    oldest_open_queued_at: null,
    worker_enabled: r.ingestions.worker_enabled,
    extractor_configured: r.ingestions.extractor_configured,
    without_document_listed: [],
  },
  ingest_findings: r.ingest_findings.filter((f) => !JOB_CODES.includes(f.code)),
  coverage: {
    rules_run: r.coverage.rules_run.filter((c) => !JOB_CODES.includes(c)),
    rules_skipped: [...r.coverage.rules_skipped, ...JOB_CODES],
    complete: false,
  },
  skipped: [...r.skipped, "ingestions: TimeoutError"],
});

// store.MAX_GROUPS: a report reads at most this many group rows, and says when there are more.
const MAX_GROUPS = 1000;

/** inspect.py _counts: one group row per (format, scheme), folded by `key`, most live first,
 *  then most deleted, then by name. */
const foldGroups = (groups: Array<[string, string, number, number]>, key: 0 | 1) => {
  const totals = new Map<string, [number, number]>();
  for (const g of groups) {
    const t = totals.get(g[key]) ?? [0, 0];
    totals.set(g[key], [t[0] + g[2], t[1] + g[3]]);
  }
  return [...totals.entries()]
    .sort(([a, [al, ad]], [b, [bl, bd]]) => bl - al || bd - ad || (a < b ? -1 : 1))
    .map(([name, [live, deleted]]) => ({ name, live, deleted }));
};

/** The base's two live documents and more than MAX_GROUPS format and scheme pairs, the rest
 *  deleted documents: the read keeps the first MAX_GROUPS rows (most live first) and totals
 *  none of them. */
const documentsCut = (): KnowledgeInspection => {
  type Group = [string, string, number, number];
  const groups: Group[] = [
    ["markdown", "https", 1, 1] as Group,
    ["pdf", "https", 1, 0] as Group,
    ...Array.from({ length: MAX_GROUPS - 1 }, (_, i): [string, string, number, number] => [
      `f${i % 25}`,
      `s${Math.floor(i / 25)}`,
      0,
      1,
    ]),
  ].slice(0, MAX_GROUPS);
  return {
    ...KNOWLEDGE,
    documents: {
      ...KNOWLEDGE.documents,
      live: null,
      deleted: null,
      earlier_revisions: null,
      unnumbered: null,
      by_format: foldGroups(groups, 0),
      by_source_scheme: foldGroups(groups, 1),
    },
    listed_complete: false,
    skipped: [`documents: more than ${MAX_GROUPS} format and scheme pairs; the counts cover the first`],
  };
};

/** The spec lost more than MAX_GROUPS capabilities: the coverage read keeps the first
 *  MAX_GROUPS, most documents first, then by name. */
const extractionCut = (): KnowledgeInspection => {
  const names = Array.from({ length: MAX_GROUPS + 1 }, (_, i) => `cap${i}`);
  const [spec, notes] = KNOWLEDGE.listed;
  return {
    ...KNOWLEDGE,
    listed: [
      { ...spec, extraction: { unit: "pages", total: 10, extracted: 9, failed: 1, lost: names, complete: false } },
      notes,
    ],
    extraction: {
      documents: 1,
      incomplete: 1,
      lost: [...names]
        .sort()
        .slice(0, MAX_GROUPS)
        .map((name) => ({ name, live: 1, deleted: 0 })),
    },
    skipped: [`extraction: more than ${MAX_GROUPS} lost capabilities; the counts cover the first`],
  };
};

const QUEUED = "https://example.com/first.pdf";
const PENDING: IngestJob = {
  ...JOB,
  ingestion_id: "ing-2",
  state: "pending",
  ended_at: null,
  updated_at: "2026-11-01T09:00:00",
  next_attempt_at: "2026-11-01T09:00:30",
  attempts: 0,
};

/** An empty namespace whose first ingest is queued: one source, its job pending, which
 *  the backlog check counts, with no live document. */
const firstIngestQueued = (): KnowledgeInspection => ({
  ...emptyNamespace(),
  ingestions: {
    ...NO_JOBS,
    sources: 1,
    without_document: 1,
    pending: 1,
    oldest_open_queued_at: PENDING.queued_at,
    without_document_listed: [sourceJobs(QUEUED, PENDING)],
  },
  ingest_findings: INGEST_CODES.map(([c, k]) =>
    c === "ingest_backlog"
      ? ingestFinding(c, k, 1, [[QUEUED, "ing-2"]], 1)
      : ingestFinding(c, k, c === "extraction_incomplete" ? 0 : 1),
  ),
});

// How a job that one check counts looks: its state, failure class and error code.
const HIT_JOB: Record<string, { state: string; failure_class: string; error_code: string }> = {
  ingest_backlog: { state: "pending", failure_class: "", error_code: "" },
  extractor_unavailable: { state: "failed", failure_class: "extractor_unavailable", error_code: "format_unavailable" },
  ingest_missing_access: { state: "failed", failure_class: "missing_access", error_code: "original_gone" },
  ingest_content_rejected: { state: "failed", failure_class: "bad_content", error_code: "corrupt" },
  ingest_failed_on_server: { state: "failed", failure_class: "retries_exhausted", error_code: "retries_exhausted" },
};
const HIT_SOURCES = ["https://example.com/a.pdf", "https://example.com/b.pdf", "https://example.com/c.pdf"];

/** Exactly one ingest check hits. A job check: three more sources, first ingests with no
 *  live document, whose jobs it counts, and the spec's extraction whole, so the extraction
 *  check is clean. extraction_incomplete: the base, where only it hits. */
const oneIngestHit = (code: string): KnowledgeInspection => {
  if (code === "extraction_incomplete") return KNOWLEDGE;
  const hit = HIT_JOB[code];
  const jobs = HIT_SOURCES.map((uri, k) => {
    const open = hit.state === "pending";
    return sourceJobs(uri, {
      ...JOB,
      ingestion_id: `ing-h${k}`,
      ...hit,
      ended_at: open ? null : JOB.ended_at,
      next_attempt_at: open ? "2026-11-01T09:00:30" : null,
    });
  });
  const [spec, notes] = KNOWLEDGE.listed;
  return {
    ...KNOWLEDGE,
    listed: [
      {
        ...spec,
        extraction: { unit: "pages", total: 10, extracted: 10, failed: 0, lost: [], complete: true },
        jobs: sourceJobs(SPEC, { ...JOB, state: "ready" }),
      },
      notes,
    ],
    ingestions: {
      ...NO_JOBS,
      sources: 4,
      without_document: 3,
      ready: 1,
      pending: hit.state === "pending" ? 3 : 0,
      failed: hit.state === "failed" ? 3 : 0,
      oldest_open_queued_at: hit.state === "pending" ? JOB.queued_at : null,
      without_document_listed: jobs,
    },
    extraction: { documents: 1, incomplete: 0, lost: [] },
    ingest_findings: INGEST_CODES.map(([c, k]) =>
      c === code
        ? ingestFinding(
            c,
            k,
            4,
            jobs.map((j) => [j.source_uri, j.current.ingestion_id]),
            3,
          )
        : ingestFinding(c, k, c === "extraction_incomplete" ? 1 : 4),
    ),
  };
};

const namespaceInventory = vi.fn(async () => INVENTORY);
const inspectKnowledge = vi.fn(async () => KNOWLEDGE);
const inspectMemories = vi.fn(async (_s: unknown, subject: MemoryInspection["subject"], id: string) =>
  inspection(subject, id),
);
const tuneNamespace = vi.fn(async () => TUNE);
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    namespaceInventory: (...a: unknown[]) => namespaceInventory(...(a as [])),
    inspectMemories: (...a: [unknown, MemoryInspection["subject"], string]) => inspectMemories(...a),
    inspectKnowledge: (...a: unknown[]) => inspectKnowledge(...(a as [])),
    tuneNamespace: (...a: unknown[]) => tuneNamespace(...(a as [])),
  };
});

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const viewer: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team", "other"],
};

beforeEach(() => {
  vi.clearAllMocks();
  resetNamespaceRoleCache();
  useStore.setState({
    authed: true,
    whoami: viewer,
    myNamespaces: [row("team", "viewer"), row("other", "viewer")],
    identity: { org: "dev", namespace: "team", agent: "loom-ui" },
  });
});
afterEach(() => {
  cleanup();
  useStore.setState({
    identity: null,
    whoami: null,
    myNamespaces: null,
    inspectedMemoryKey: null,
    inspectedAgent: "",
    inspectedOwner: null,
  });
});

const switchNamespace = () =>
  act(() => {
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
  });

describe("Assistant: starters and scope", () => {
  it("explains itself, offers the starters and reads nothing on mount", async () => {
    const { container } = render(<Assistant />);
    expect(screen.getByRole("heading", { level: 1, name: "Assistant" })).toBeInTheDocument();
    for (const name of [
      "Show relationships between facts",
      "What is in this project?",
      "Analyze one user's memories",
      "Who are the users in this project?",
      "Configure this project",
    ])
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getByText(/No drafts yet/)).toBeInTheDocument();
    expect(namespaceInventory).not.toHaveBeenCalled();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("offers no read to a caller whose role on the namespace is unknown", async () => {
    useStore.setState({ myNamespaces: [] });
    render(<Assistant />);
    const starter = screen.getByRole("button", { name: "Configure this project" });
    expect(starter).toHaveAttribute("aria-disabled", "true");
    expect(starter).toHaveAccessibleDescription("Your role on this namespace is not known.");
    await userEvent.click(starter);
    expect(tuneNamespace).not.toHaveBeenCalled();
  });
});

describe("Assistant: relationships", () => {
  it("opens the graph only from an entity", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Show relationships between facts" }));
    const open = screen.getByRole("button", { name: "Open the graph" });
    expect(open).toHaveAccessibleDescription("Enter an entity to walk from.");
    await userEvent.type(screen.getByLabelText("Entity"), "invoices");
    expect(screen.getByRole("button", { name: "Open the graph" })).not.toHaveAttribute("aria-disabled");
    await userEvent.click(screen.getByRole("button", { name: "Open the graph" }));
    expect(useStore.getState().view).toBe("graph");
  });
});

describe("Assistant: inventory", () => {
  it("shows what the namespace holds, with a section it could not count said so", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    const table = await screen.findByRole("table", { name: "What this namespace holds" });
    expect(within(table).getByText("42")).toBeInTheDocument();
    expect(within(table).getByTestId("inventory-incomplete")).toHaveTextContent("read failed: TimeoutError");
    expect(screen.getByText(/Model: not enabled/)).toBeInTheDocument();
    expect(namespaceInventory).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, expect.anything());
  });

  it("lists members apart from the identities observed on rows, and says when members are incomplete", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    const list = await screen.findByTestId("assistant-populations");
    const members = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "members");
    expect(members).toHaveTextContent(/people holding an active grant/);
    expect(members).toHaveTextContent(
      "Not complete: shared with everyone, a group or an agent: those members are not counted",
    );
    const subjects = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "subjects");
    expect(subjects).toHaveTextContent("ada12");
  });

  it("says a population lists only its largest identities", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      populations: INVENTORY.populations.map((p) => (p.population === "subjects" ? { ...p, total: 340 } : p)),
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    const list = await screen.findByTestId("assistant-populations");
    const subjects = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "subjects");
    expect(subjects).toHaveTextContent("The 1 with the most rows are shown.");
  });

  it("says the counts are bound to the user the token is pinned to", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    expect(await screen.findByTestId("inventory-scope")).toHaveTextContent(
      "Memories, runs and the agents, authors and subjects on rows are counted for user ada only, as your token reads; documents and members are the namespace's.",
    );
  });

  it("offers no inspection or drafts once the inventory shows a token pinned to one user", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("inventory-scope");
    const configure = screen.getByRole("button", { name: "Configure this project" });
    expect(configure).toHaveAccessibleDescription(/bound to one user or session/);
    await userEvent.click(configure);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "What is in this project?" })).not.toHaveAttribute("aria-disabled");
  });

  it("offers no inspection or drafts to a token whose own claim pins it, before any read", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ sub: "ada", read_user_id: "ada" })}.sig`);
    try {
      render(<Assistant />);
      const configure = screen.getByRole("button", { name: "Configure this project" });
      expect(configure).toHaveAccessibleDescription(/bound to one user or session/);
      await userEvent.click(configure);
      await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
      expect(tuneNamespace).not.toHaveBeenCalled();
      expect(inspectMemories).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it.each([
    ["own_session", { read_scope: "own_session", read_user_id: "ada" }],
    ["own_user without a pinned user", { read_scope: "own_user" }],
  ])("offers no inventory to a token scoped %s, which this page cannot complete", async (_label, claims) => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ sub: "ada", ...claims })}.sig`);
    try {
      render(<Assistant />);
      for (const name of ["What is in this project?", "Who are the users in this project?"]) {
        const starter = screen.getByRole("button", { name });
        expect(starter).toHaveAccessibleDescription(
          "Your token's read scope needs a user or session id this page does not send.",
        );
        await userEvent.click(starter);
      }
      expect(namespaceInventory).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("drops a pinned token's answer once the page is signed in again with a namespace token", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    const token = (claims: object) => `${b64({ alg: "HS256" })}.${b64({ sub: "ada", ...claims })}.sig`;
    localStorage.setItem("loom_token_v1", token({ read_user_id: "ada" }));
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
    });
    try {
      render(<Assistant />);
      await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
      await screen.findByTestId("inventory-scope");
      localStorage.setItem("loom_token_v1", token({}));
      act(() => {
        useStore.setState({ whoami: { ...viewer } });
      });
      await waitFor(() => expect(screen.queryByTestId("inventory-scope")).toBeNull());
      expect(screen.getByRole("button", { name: "Configure this project" })).not.toHaveAttribute("aria-disabled");
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("adds no scope note when the counts cover the namespace", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    expect(screen.queryByTestId("inventory-scope")).toBeNull();
  });

  it("reads again on every starter click", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    await userEvent.click(screen.getByRole("button", { name: "Who are the users in this project?" }));
    await screen.findByTestId("assistant-populations");
    expect(namespaceInventory).toHaveBeenCalledTimes(2);
  });

  it("says when the inventory cannot be read", async () => {
    namespaceInventory.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the inventory");
  });

  it("shows nothing of the old namespace's inventory on the first render after a switch", async () => {
    const { rerender } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
    rerender(<Assistant />);
    expect(screen.queryByTestId("assistant-inventory")).toBeNull();
  });

  it("closes the inventory panel when the namespace changes", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "What is in this project?" }));
    await screen.findByTestId("assistant-inventory");
    switchNamespace();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "What this namespace holds" })).toBeNull());
  });
});

describe("Assistant: inspection", () => {
  it("inspects a user: the rows filed under the user, apart from the rest", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Analyze one user's memories" }));
    expect(screen.getByLabelText("User id")).toHaveFocus();
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(inspectMemories).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "user", "ada", expect.anything());
    expect(within(result).getByRole("heading", { name: /Filed under this user/ })).toBeInTheDocument();
    expect(within(result).getByText("ada")).toHaveClass("break-all");
    expect(within(result).getByText(OWN_USER)).toBeInTheDocument();
    expect(within(result).getByRole("heading", { name: /Every other live row of the namespace/ })).toBeInTheDocument();
    // A listed row opens in the inspector under its writer's agent, user and session.
    await userEvent.click(within(result).getByRole("button", { name: "Open memory billing/invoices" }));
    expect(useStore.getState()).toMatchObject({
      inspectedMemoryKey: "billing/invoices",
      inspectedAgent: "claude-code",
      inspectedOwner: { user_id: "ada", session_id: "-" },
    });
    expect(within(result).getByText("Statuses: active 1")).toBeInTheDocument();
    expect(within(result).getByText("3 versions")).toBeInTheDocument();
    expect(result).not.toHaveTextContent(String(NS_VERSION).slice(0, 10));
    expect(within(result).getByText("No live rows.")).toBeInTheDocument();
    expect(within(result).getByTestId("inspect-policy")).toHaveTextContent(
      "not shown (only an org admin reads the operator extraction policy)",
    );
    expect(within(result).getByText("No calls in the last 7 days.")).toBeInTheDocument();
  });

  it.each([
    ["Agent", "agent", "claude-code", "Written by this agent"],
    ["Session", "session", "s-1", "Written in this session"],
  ] as const)("inspects a %s with its own heading", async (label, subject, id, heading) => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    await userEvent.type(screen.getByLabelText(`${label} id`), id);
    await userEvent.click(screen.getByRole("button", { name: "Inspect" }));
    const result = await screen.findByTestId("assistant-inspection");
    expect(inspectMemories).toHaveBeenCalledWith(expect.anything(), subject, id, expect.anything());
    expect(within(result).getByRole("heading", { name: new RegExp(heading) })).toBeInTheDocument();
  });

  it("says a view lists only its newest rows, and a view it could not count is not empty", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: view(OWN_USER, [ROW], 40),
      others: view(OTHERS_USER, []),
      skipped: ["others.counts: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("Newest 1 of 40 listed.")).toBeInTheDocument();
    expect(within(result).getByText("The rows could not be counted.")).toBeInTheDocument();
    expect(within(result).queryByText("No live rows.")).toBeNull();
  });

  it("states the activity window, and the ledger's retention beside the calls", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      activity: {
        window_seconds: 30 * 86_400,
        calls: [{ operation: "memory.search", calls: 9, succeeded: 8 }],
        first_at: "2026-10-20T10:00:00",
        last_at: "2026-11-01T10:00:00",
        gap: "The ledger keeps calls for a 7-day window, so older ones are not counted.",
      },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText(/Last 30 days, succeeded of called: memory.search 8\/9/)).toBeInTheDocument();
    expect(
      within(result).getByText("The ledger keeps calls for a 7-day window, so older ones are not counted."),
    ).toBeInTheDocument();
  });

  it("says which settings and calls could not be read, never their defaults", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: view(OWN_USER, [], 40),
      settings: {
        ...inspection("user", "ada").settings,
        guide_revision: 0,
        guide_summary: "",
        categories_registered: 0,
      },
      skipped: [
        "own.newest: TimeoutError",
        "settings.guide: TimeoutError",
        "settings.categories: TimeoutError",
        "activity: TimeoutError",
      ],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("The rows could not be listed.")).toBeInTheDocument();
    expect(within(result).queryByText(/Newest 0 of/)).toBeNull();
    expect(within(result).getByText("The guide could not be read.")).toBeInTheDocument();
    expect(within(result).queryByText(/Guide revision none/)).toBeNull();
    expect(within(result).getByText("The registered categories could not be read.")).toBeInTheDocument();
    expect(within(result).getByText("The calls could not be read.")).toBeInTheDocument();
    expect(within(result).queryByText(/No calls in the last/)).toBeNull();
  });

  it("says a view's tags and categories, and the ledger's retention, could not be read", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      skipped: ["own.tags: TimeoutError", "own.categories: TimeoutError", "activity.retention: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText("Tags: could not be read")).toBeInTheDocument();
    expect(within(result).getByText("Categories: could not be read")).toBeInTheDocument();
    expect(within(result).getByText(/The ledger's retention could not be read/)).toBeInTheDocument();
  });

  it.each([
    ["", "none set"],
    ["Keep costs.\nKeep ages.", "Keep costs.\nKeep ages."],
  ])("shows an org admin the operator policy %j as %j", async (policy, shown) => {
    const base = inspection("user", "ada");
    inspectMemories.mockResolvedValueOnce({
      ...base,
      settings: { ...base.settings, extraction_policy: policy, extraction_policy_unavailable: "" },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const line = await screen.findByTestId("inspect-policy");
    if (policy) expect(within(line).getByTestId("inspect-policy-text").textContent).toBe(shown);
    else expect(line).toHaveTextContent(`Operator extraction policy: ${shown}`);
  });

  it("keeps the rows, tags and categories it read when the count read failed", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: { ...view(OWN_USER, [ROW], 0), top_tags: [{ name: "billing", rows: 1 }] },
      skipped: ["own.counts: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const own = (await screen.findByTestId("assistant-inspection")).querySelector(
      '[data-view="inspect-own"]',
    ) as HTMLElement;
    expect(within(own).getByText("The rows could not be counted.")).toBeInTheDocument();
    expect(within(own).getByText("Tags: billing 1")).toBeInTheDocument();
    expect(within(own).getByRole("button", { name: "Open memory billing/invoices" })).toBeInTheDocument();
    expect(within(own).queryByText(/Newest/)).toBeNull();
  });

  it("labels a view's tags as the top 20 when the server's cap is reached", async () => {
    const tags = Array.from({ length: 20 }, (_, n) => ({ name: `t${n}`, rows: 1 }));
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      own: { ...view(OWN_USER, [ROW]), top_tags: tags },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText(/^Top 20 tags: t0 1/)).toBeInTheDocument();
  });

  it("shows a guide that was never written as none written", async () => {
    const base = inspection("user", "ada");
    inspectMemories.mockResolvedValueOnce({
      ...base,
      settings: { ...base.settings, guide_revision: 0, guide_summary: "" },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(await screen.findByText("No guide written")).toBeInTheDocument();
  });

  it("does not inspect without an id", async () => {
    render(<Assistant />);
    const inspect = screen.getByRole("button", { name: "Inspect" });
    expect(inspect).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(inspect);
    await userEvent.type(screen.getByLabelText("User id"), "{Enter}");
    expect(inspectMemories).not.toHaveBeenCalled();
  });

  it("says when the inspection cannot be read", async () => {
    inspectMemories.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the inspection");
  });

  it("drops the inspection when the namespace changes", async () => {
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    await screen.findByTestId("assistant-inspection");
    switchNamespace();
    await waitFor(() => expect(screen.queryByTestId("assistant-inspection")).toBeNull());
  });
});

describe("Assistant: review changes", () => {
  it("renders each draft with its diff, evidence and the call that would apply it, and never applies", async () => {
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", { name: /Register a category for the tag/ });
    expect(within(card).getByText("Draft, not approved")).toBeInTheDocument();
    expect(card.querySelector('[data-side="current"]')).toHaveTextContent("(empty)");
    expect(card.querySelector('[data-side="proposed"]')).toHaveTextContent("billing");
    expect(within(card).getByText(/12 supporting · 4 already filed under another category/)).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Open memory billing/invoices" }));
    expect(useStore.getState()).toMatchObject({
      inspectedMemoryKey: "billing/invoices",
      inspectedAgent: "claude-code",
      inspectedOwner: { user_id: "ada", session_id: "-" },
    });
    expect(within(card).getByTestId("affected-agents")).toHaveTextContent("claude-codeand 2 more");
    expect(within(card).getByText("namespace.register_category")).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"name": "billing"');
    const apply = within(card).getByRole("button", { name: "Apply" });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    expect(apply).toHaveAccessibleDescription(/A writer on this namespace sends namespace.register_category/);
    await userEvent.click(apply);
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "audit.telemetry_unavailable: window within the ledger lag",
    );
    expect(tuneNamespace).toHaveBeenCalledTimes(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows a guide draft's revision and arguments, and says when the writers could not be read", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", { name: /Name the key prefixes the guide leaves out/ });
    expect(within(card).getByText(/Current \(revision 2\)/)).toBeInTheDocument();
    expect(within(card).getByText(/^Evidence: 12 supporting$/)).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"expected_revision": 2');
    expect(within(card).getByText(/the namespace's writers could not be read/)).toBeInTheDocument();
  });

  it("captions a guide with no revision yet without a revision", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [{ ...GUIDE, current: "", current_revision: 0 }] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByText("Current")).toBeInTheDocument();
    expect(within(review).queryByText(/revision 0/)).toBeNull();
  });

  it("says that each writer sends the extraction instructions itself", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", {
      name: /Send extraction instructions for the shapes the audit found/,
    });
    expect(
      within(card).getByText("Each writer adds these arguments to its own memory.set_from_messages requests:"),
    ).toBeInTheDocument();
    expect(
      within(card).getByText(/episodic_blobs · 5 supporting · at least 25 unflagged rows it must keep working for/),
    ).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"extraction_instructions"');
    expect(within(card).getByRole("button", { name: "Apply" })).toHaveAccessibleDescription(
      /no single call applies this/,
    );
    expect(within(card).getByTestId("affected-agents")).toHaveTextContent("Affected agents:claude-code");
  });

  it("does not claim the evidence supports no change when a read failed", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: ["audit: TimeoutError"] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent("audit: TimeoutError");
  });

  it("lists what was not drafted, and why, when the evidence rules every draft out", async () => {
    const reason = "extraction: memory.set_from_messages writes no row here, so no rule is drafted for episodic_blobs";
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [reason] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(reason);
  });

  it("says the evidence supports no change when nothing was left out", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent("The evidence supports no change.");
  });

  it("lists a read skipped for a draft it still shows under Skipped, not as undrafted", async () => {
    tuneNamespace.mockResolvedValueOnce({
      ...TUNE,
      proposals: [{ ...CATEGORY, evidence: { ...CATEGORY.evidence, supporting: [] } }],
      skipped: ["category.billing.cited: TimeoutError"],
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("listitem", { name: /Register a category for the tag/ })).toBeInTheDocument();
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "Skipped:category.billing.cited: TimeoutError",
    );
    expect(within(review).queryByText(/Not drafted/)).toBeNull();
  });

  it("keeps the drafts on screen, marked busy, while they are drafted again", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    await screen.findByTestId("assistant-review");
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = screen.getByTestId("assistant-review");
    expect(review.parentElement).toHaveAttribute("aria-busy", "true");
    expect(within(review).getByText("Register a category for the tag `billing`")).toBeInTheDocument();
  });

  it("says when the drafts cannot be read", async () => {
    tuneNamespace.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the drafts");
  });

  it("is not left loading when the page comes back to a namespace it left mid-read", async () => {
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    switchNamespace();
    act(() => {
      useStore.setState({ identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    });
    await waitFor(() => expect(screen.getByText(/No drafts yet/)).toBeInTheDocument());
    expect(screen.queryByText("Loading the drafts")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    expect(await screen.findByTestId("assistant-review")).toBeInTheDocument();
  });

  it("drops a draft that answers after the namespace changed", async () => {
    let answer: (t: NamespaceTune) => void = () => {};
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>((r) => (answer = r)));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    switchNamespace();
    await act(async () => answer(TUNE));
    await waitFor(() => expect(screen.getByText(/No drafts yet/)).toBeInTheDocument());
    expect(screen.queryByTestId("assistant-review")).toBeNull();
  });
});

const pickDocuments = async () => {
  await userEvent.click(screen.getByRole("button", { name: "Documents" }));
  await userEvent.click(screen.getByRole("button", { name: "Inspect" }));
  return screen.findByTestId("assistant-documents");
};

describe("Assistant: documents", () => {
  it("inspects the namespace's documents with no id: formats, what search serves, decisions and the newest", async () => {
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Documents" }));
    expect(screen.queryByLabelText(/id$/)).toBeNull();
    const result = await pickDocuments();
    expect(inspectKnowledge).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, expect.anything());
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(within(result).getByTestId("docs-totals-line")).toHaveTextContent(
      "Live documents: 2 · deleted: 1 · earlier revisions: 3 · retired chunk versions: 4",
    );
    const formats = within(result).getByRole("table", { name: "Documents by format" });
    expect(within(formats).getByText("pdf")).toBeInTheDocument();
    expect(within(result).getByTestId("docs-searchable")).toHaveTextContent(
      "11 of 12 live chunks searchable under text-embedding-3-small · without a vector: 1",
    );
    expect(within(result).getByRole("listitem", { name: "Chunks with no vector" })).toHaveTextContent(
      "1 document of 2 (50%)",
    );
    expect(within(result).getByTestId("docs-decisions-total")).toHaveTextContent(
      "Decided: 3 (1 confirmed, 2 unconfirmed, 0 source changed, 0 with an outcome) · Proposed: none",
    );
    const spec = result.querySelector('[data-source="https://example.com/spec.pdf"]') as HTMLElement;
    expect(spec).toHaveTextContent("Chunks: 8 of 8 searchable");
    expect(spec).toHaveTextContent("Extraction: 9 of 10 pages extracted, 1 failed, lost section_path");
    expect(spec).toHaveTextContent("job ing-1: partial");
    const notes = result.querySelector('[data-source="https://example.com/notes.md"]') as HTMLElement;
    expect(notes).toHaveTextContent("Extraction: none (a text write, which no extractor read)");
    expect(within(result).getByTestId("docs-extraction")).toHaveTextContent(
      "Extraction coverage: 1 of 1 ingested document not complete · lost: section_path 1",
    );
    expect(within(result).getByText("oversized_values")).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("says an empty namespace holds no document, and lists no document sections", async () => {
    inspectKnowledge.mockResolvedValueOnce(emptyNamespace());
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByRole("status")).toHaveTextContent("This namespace holds no live document");
    expect(within(result).queryByRole("heading", { name: /Newest documents/ })).toBeNull();
    expect(within(result).queryByRole("heading", { name: "What search serves" })).toBeNull();
    expect(within(result).getByText("No knowledge.ingest job in the last 30 days.")).toBeInTheDocument();
  });

  it("shows a section it could not read as not read, never as zero", async () => {
    const base = documentsUnread(KNOWLEDGE);
    inspectKnowledge.mockResolvedValueOnce({
      ...base,
      chunks: { ...KNOWLEDGE.chunks, live: null, searchable: null, unembedded: null, by_model: [] },
      decisions: { ...KNOWLEDGE.decisions, all_documents: null },
      ingestions: { ...KNOWLEDGE.ingestions, extractor_configured: null },
      listed: [],
      skipped: [
        ...base.skipped,
        "chunks: TimeoutError",
        "listed: TimeoutError",
        "decisions: TimeoutError",
        "ingestions.extractor_configured: ValueError",
      ],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByTestId("docs-totals-line")).toHaveTextContent(
      "Live documents: not read · deleted: not read · earlier revisions: not read",
    );
    expect(within(result).getByText("The documents could not be counted by format and source.")).toBeInTheDocument();
    expect(within(result).getByText("The chunks could not be counted.")).toBeInTheDocument();
    expect(within(result).queryByTestId("docs-searchable")).toBeNull();
    expect(within(result).getByText("The decisions could not be read.")).toBeInTheDocument();
    expect(within(result).getByText("The documents could not be listed.")).toBeInTheDocument();
    expect(within(result).getByText(/extractor settings not read/)).toBeInTheDocument();
    expect(within(result).getByTestId("docs-skipped")).toHaveTextContent("Not read: documents: TimeoutError; chunks");
    expect(within(result).queryByRole("status")).toBeNull();
  });

  it.each(INGEST_CODES)(
    "shows the %s finding in its own %s group, apart from the others",
    async (code, kind, label) => {
      inspectKnowledge.mockResolvedValueOnce(oneIngestHit(code));
      render(<Assistant />);
      const result = await pickDocuments();
      const card = within(result).getByRole("listitem", { name: label });
      expect(card).toHaveAttribute("data-code", code);
      expect(card.closest("[data-kind]")).toHaveAttribute("data-kind", kind);
      expect(card).toHaveTextContent(`remedy of ${code}`);
      if (code === "extraction_incomplete") {
        expect(card).toHaveTextContent("1 document of 1 (100%)");
        expect(card).toHaveTextContent(SPEC);
        expect(card).not.toHaveTextContent("job ");
      } else {
        expect(card).toHaveTextContent("3 sources of 4 (75%) · 3 with no live document");
        for (const [k, uri] of HIT_SOURCES.entries()) expect(card).toHaveTextContent(`${uri} · job ing-h${k}`);
      }
      // Every other check is clean and named as such, never folded into this one.
      for (const [other, , otherLabel] of INGEST_CODES.filter(([c]) => c !== code)) {
        expect(result.querySelector(`li[data-code="${other}"]`)).toBeNull();
        expect(result).toHaveTextContent(otherLabel);
      }
    },
  );

  it("does not call a namespace empty when its job count could not be read", async () => {
    inspectKnowledge.mockResolvedValueOnce(jobsUnread(emptyNamespace()));
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).queryByRole("status")).toBeNull();
    expect(within(result).getByText("The knowledge.ingest jobs could not be read.")).toBeInTheDocument();
    expect(within(result).queryByText("The job checks did not run.")).toBeNull();
  });

  it("says there is no live document to list while a first ingest is queued", async () => {
    inspectKnowledge.mockResolvedValueOnce(firstIngestQueued());
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).queryByRole("status")).toBeNull();
    expect(within(result).getByText("No live document to list.")).toBeInTheDocument();
    expect(within(result).queryByRole("list", { name: "Newest documents" })).toBeNull();
  });

  it("says a listing may be partial when the documents total was not read", async () => {
    inspectKnowledge.mockResolvedValueOnce(documentsUnread(KNOWLEDGE));
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByText(/listed; the total was not counted\./)).toBeInTheDocument();
  });

  it("shows the documents a cut read counted, with the server's note, not as unread", async () => {
    const note = `more than ${MAX_GROUPS} format and scheme pairs; the counts cover the first`;
    inspectKnowledge.mockResolvedValueOnce(documentsCut());
    render(<Assistant />);
    const result = await pickDocuments();
    expect(
      within(within(result).getByRole("table", { name: "Documents by format" })).getByText("pdf"),
    ).toBeInTheDocument();
    expect(within(result).getByTestId("docs-documents-note")).toHaveTextContent(`Note: ${note}`);
    expect(within(result).getByTestId("docs-totals-line")).toHaveTextContent("Live documents: not totalled");
    expect(within(result).getByText(/listed; the total was not counted\./)).toBeInTheDocument();
    expect(within(result).queryByText("The documents could not be counted by format and source.")).toBeNull();
  });

  it("shows the extraction coverage a cut read counted, with the server's note", async () => {
    const note = `more than ${MAX_GROUPS} lost capabilities; the counts cover the first`;
    inspectKnowledge.mockResolvedValueOnce(extractionCut());
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByTestId("docs-extraction")).toHaveTextContent("lost: cap0 1, cap1 1, cap10 1");
    expect(within(result).getByTestId("docs-extraction-note")).toHaveTextContent(`Note: ${note}`);
    expect(within(result).queryByText("The extraction coverage could not be read.")).toBeNull();
  });

  it("says the checks did not run only when the reads they run on failed", async () => {
    // The chunk, job and coverage reads all failed: every check is skipped.
    const jobs = jobsUnread(KNOWLEDGE);
    inspectKnowledge.mockResolvedValueOnce({
      ...jobs,
      findings: [],
      extraction: { documents: null, incomplete: null, lost: [] },
      ingest_findings: [],
      coverage: { rules_run: [], rules_skipped: ALL_CHECKS, complete: false },
      skipped: ["findings: TimeoutError", ...jobs.skipped, "extraction: TimeoutError"],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByText("The chunk checks did not run.")).toBeInTheDocument();
    expect(within(result).getByText("The job checks did not run.")).toBeInTheDocument();
    expect(within(result).getByText("The extraction coverage could not be read.")).toBeInTheDocument();
    expect(within(result).queryByTestId("docs-extraction-note")).toBeNull();
  });

  it("offers documents to a token pinned to one user, which the server answers in scope", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem("loom_token_v1", `${b64({ alg: "HS256" })}.${b64({ sub: "ada", read_user_id: "ada" })}.sig`);
    try {
      render(<Assistant />);
      await pickDocuments();
      expect(inspectKnowledge).toHaveBeenCalledTimes(1);
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("offers no documents to a token scoped own_session, which this page cannot complete", async () => {
    const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    localStorage.setItem(
      "loom_token_v1",
      `${b64({ alg: "HS256" })}.${b64({ sub: "ada", read_scope: "own_session", read_user_id: "ada" })}.sig`,
    );
    try {
      render(<Assistant />);
      await userEvent.click(screen.getByRole("button", { name: "Documents" }));
      const inspect = screen.getByRole("button", { name: "Inspect" });
      expect(inspect).toHaveAccessibleDescription(
        "Your token's read scope needs a user or session id this page does not send.",
      );
      await userEvent.click(inspect);
      expect(inspectKnowledge).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("brings the id field back, focused, when a user's memories are asked for from Documents", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Documents" }));
    await userEvent.click(screen.getByRole("button", { name: "Analyze one user's memories" }));
    expect(screen.getByLabelText("User id")).toHaveFocus();
  });

  it("says when the documents cannot be read", async () => {
    inspectKnowledge.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Documents" }));
    await userEvent.click(screen.getByRole("button", { name: "Inspect" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the documents");
  });

  it("drops the documents when the namespace changes", async () => {
    render(<Assistant />);
    await pickDocuments();
    switchNamespace();
    await waitFor(() => expect(screen.queryByTestId("assistant-documents")).toBeNull());
  });
});

const STATS = (size: number, stride: number, chunks: number, cut: number, tokens: number) => ({
  size,
  stride,
  chunks,
  min_chars: 40,
  median_chars: Math.round(size * 0.8),
  p90_chars: size,
  max_chars: size,
  sections: 40,
  sections_cut: cut,
  max_input_tokens: tokens,
  over_input_limit: 0,
});

// server/knowledge/app/tune.py _chunking_proposal: settings.set through the admin route,
// the stride after it.
const CHUNKING: TuneProposal = {
  id: "c1",
  target: "settings.knowledge_chunking",
  scope: "every namespace of this deployment (a deployment setting)",
  title: "Cut documents into chunks of 2000 characters",
  rationale: "Of 30 documents of dev/team cut again at the current values, the size cuts 18 of 40 sections.",
  current: "LOOM_KNOWLEDGE_CHUNK_SIZE=1000, LOOM_KNOWLEDGE_CHUNK_STRIDE=800",
  proposed: "LOOM_KNOWLEDGE_CHUNK_SIZE=2000, LOOM_KNOWLEDGE_CHUNK_STRIDE=1600",
  current_revision: null,
  evidence: {
    finding_codes: [],
    supporting_count: 12,
    supporting: [],
    counter_count: 18,
    documents: ["https://example.com/spec.pdf"],
  },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
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
  preview: { before: STATS(1000, 800, 120, 18, 300), after: STATS(2000, 1600, 64, 2, 560) },
  limits: "The settings are the deployment's.",
};

// _routes: an environment variable with placeholders to fill in.
const ROUTES: TuneProposal = {
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
    counter_count: 5,
    documents: ["https://example.com/a.pdf", "https://example.com/b.pdf"],
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
    guard: "None: an environment setting has no revision.",
  },
  apply_also: [],
  preview: null,
  limits: "A URL is not shown.",
};

const SAMPLE = {
  cap: 200,
  population: 30,
  documents: 30,
  unread: 0,
  load_cap: 20_000_000,
  embedder_model: "text-embedding-3-small",
  input_token_limit: 8191,
  current: STATS(1000, 800, 120, 18, 300),
};

describe("Assistant: document drafts", () => {
  it("shows a chunking draft's before and after on the sample, its cap, and the admin route that applies it", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [CHUNKING], chunking: SAMPLE });
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByTestId("tune-sample")).toHaveTextContent(
      "Document sample: 30 of 30 live documents re-chunked at the current values (cap 200",
    );
    expect(within(review).getByTestId("tune-sample")).toHaveTextContent("input limit 8,191 tokens");
    const card = within(review).getByRole("listitem", { name: /Cut documents into chunks of 2000/ });
    expect(within(card).getByText("Chunk size and stride (deployment setting)", { exact: false })).toBeInTheDocument();
    const preview = within(card).getByRole("table", { name: /Before and after/ });
    const cut = within(preview).getByText("Sections cut by size").closest("tr") as HTMLElement;
    expect(cut).toHaveTextContent("18 of 40");
    expect(cut).toHaveTextContent("2 of 40");
    expect(within(card).getByText(/12 supporting · 18 sampled documents it does not flag/)).toBeInTheDocument();
    expect(within(card).getByRole("list", { name: /Documents cited for/ })).toHaveTextContent(
      "https://example.com/spec.pdf",
    );
    expect(within(card).getAllByText("POST /v1/admin/settings.set")).toHaveLength(2);
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"name": "knowledge_chunk_size"');
    expect(within(card).getByTestId("proposal-also-args")).toHaveTextContent('"name": "knowledge_chunk_stride"');
    expect(within(card).getByText(/Applies to documents written after the change/)).toBeInTheDocument();
    const apply = within(card).getByRole("button", { name: "Apply" });
    expect(apply).toHaveAttribute("aria-disabled", "true");
    expect(apply).toHaveAccessibleDescription(/A platform_admin sends POST \/v1\/admin\/settings.set/);
    await userEvent.click(apply);
    expect(tuneNamespace).toHaveBeenCalledTimes(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows an extractor route set in the environment, with its placeholders to fill in and the restart", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [ROUTES], chunking: null });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Configure this project" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).queryByTestId("tune-sample")).toBeNull();
    const card = within(review).getByRole("listitem", { name: /Route pdf to an extractor/ });
    expect(card.querySelector('[data-via="environment"]')).not.toBeNull();
    expect(within(card).getByText("Value to set, once each placeholder is filled in:")).toBeInTheDocument();
    expect(within(card).getByText("LOOM_DOCUMENT_EXTRACTORS_JSON (environment)")).toBeInTheDocument();
    expect(
      within(card).getByText(/extractor_unavailable · 2 supporting · 5 documents of these formats extracted whole/),
    ).toBeInTheDocument();
    expect(within(card).getByRole("list", { name: /Documents cited for/ })).toHaveTextContent(
      "https://example.com/b.pdf",
    );
    expect(within(card).getByRole("button", { name: "Apply" })).toHaveAccessibleDescription(
      /A platform_admin sets LOOM_DOCUMENT_EXTRACTORS_JSON in the deployment; it is in force once each server and worker restarts/,
    );
    expect(within(card).queryByTestId("proposal-also")).toBeNull();
  });
});
