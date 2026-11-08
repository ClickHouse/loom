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
import { listMine } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { Assistant } from "./Assistant";

// A row version is a nanosecond stamp (server/memory/store/rows.py), past what a JavaScript
// number holds exactly: the page never prints it.
const NS_VERSION = Number("1762156800123456789");

// The Assistant page reads namespace.inventory on load and memory.inspect, knowledge.inspect
// and namespace.tune on a click, all read-only, and never applies a draft: it renders what
// they answer, with what they could not read said so. The fixtures are the
// shapes the server sends.

const INVENTORY: NamespaceInventory = {
  scope: { org: "dev", namespace: "team", read_scope: "namespace", user_id: null, session_id: null },
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
  "Live memories saved for this user, whoever wrote them: what a token limited to this user reads. Memories promoted for every agent of the namespace are not counted.";
const OTHERS_USER =
  "Every other live memory of the namespace. A member reads these too; a token limited to this user does not. Memories promoted for every agent of the namespace are not counted.";

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
  // server/memory/app/inspect.py _DEFINITION_REFS["user"].
  own: { ...view(OWN_USER, [ROW]), refs: ["user_id", "read_scope own_user", "memory.promote"] },
  others: { ...view(OTHERS_USER, []), refs: ["user_id", "read_scope own_user", "memory.promote"] },
  activity: { window_seconds: 7 * 86_400, calls: [], first_at: null, last_at: null, gap: "" },
  settings: {
    guide_revision: 2,
    guide_summary: "key layout",
    categories_registered: 1,
    extraction_policy: null,
    extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
    extraction_instructions:
      "Each request that extracts memories from messages may send extraction instructions; no user, agent or session setting stores them.",
    extraction_instructions_refs: ["memory.set_from_messages", "extraction_instructions"],
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

/** A memory draft as the current server sends it: the write is an op, nothing follows it,
 *  no preview, no documents cited. A server a release behind leaves these fields out. */
const served = (p: TuneProposal): TuneProposal => ({
  ...p,
  evidence: { ...p.evidence, documents: [] },
  apply: { ...p.apply, via: "op" },
  apply_also: [],
  preview: null,
});

// A category draft cites the tagged rows and counts those another category holds.
const CATEGORY: TuneProposal = served({
  id: "2ef4b21f4c7eb774",
  target: "namespace.category",
  scope: "namespace dev/team",
  title: "Register a category for the tag `billing`",
  rationale:
    "12 live memories carry the tag `billing` and no category is named after it; 4 of them are filed under another category. A registered category is what a category search finds, and extraction offers the first 32 by name as tags for new memories.",
  current: "",
  current_revision: null,
  proposed: "billing",
  evidence: { finding_codes: [], supporting_count: 12, supporting: [CITED], counter_count: 4 },
  affected_agents: ["claude-code"],
  affected_agents_total: 3,
  effect: "future",
  historical:
    "Registering files no memory: memories already tagged join the category only when each is assigned to it, a separate action per memory.",
  apply: {
    op: "namespace.register_category",
    args: { org: "dev", namespace: "team", name: "billing", description: "Memories tagged `billing`." },
    args_complete: true,
    required_role: "writer",
    guard:
      "None: registering a name that exists updates that category, so list the categories again just before applying.",
  },
  limits: "A tag used for two meanings would need two categories, not one.",
  refs: [
    "memory.set_from_messages",
    "namespace.search_categories",
    "memory.assign_category",
    "namespace.list_categories",
  ],
});

// _guide_lines appends the prefixes an existing guide leaves out.
const GUIDE_CURRENT = "# team\n\n## Key layout\n\n- `notes/…`: 9 memories\n";
const GUIDE_PROPOSED = `${GUIDE_CURRENT}\n## Key layout\n\n- \`billing/…\`: 12 memories\n`;

// A guide draft cites no row and counts none; here the writers could not be read.
const GUIDE: TuneProposal = served({
  id: "12b965a8ed32f1da",
  target: "namespace.guide",
  scope: "namespace dev/team",
  title: "Name the key prefixes the guide leaves out",
  rationale:
    "1 key prefix holding memories is not named in the guide. A writer reads the guide before its first write, so it names the layout new memories should follow; the counts are today's live memories.",
  current: GUIDE_CURRENT,
  current_revision: 2,
  proposed: GUIDE_PROPOSED,
  evidence: { finding_codes: [], supporting_count: 12, supporting: [], counter_count: null },
  affected_agents: [],
  affected_agents_total: null,
  effect: "future",
  historical: "A guide changes no stored memory; it shapes the writes that read it.",
  refs: ["namespace.guide_get", "expected_revision"],
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
      "The guide's revision the draft was read at: a guide written since then refuses the write and answers with its current text to merge into.",
  },
  limits:
    "The draft lists what the memories show, not what they mean; a person writes what each prefix and tag is for.",
});

// The extraction draft answers audit findings and reaches the agents extraction wrote for.
const INSTRUCTIONS: TuneProposal = served({
  id: "67a699bd202bbfee",
  target: "extraction_instructions.request",
  scope: "each request that extracts memories from messages into dev/team and sends these instructions",
  title: "Send extraction instructions for the shapes the audit found",
  rationale:
    "The audit flagged episodic blobs: 5 of 30 memories (counts over every writer). Extraction wrote 30 of the live memories here; 1 of the memories the audit cited came from it. No user, agent or session setting stores instructions, so a writer sends them per request.",
  current: "",
  current_revision: null,
  proposed: "- Keep an episode to a short summary; a run's full output belongs in offloaded context, not in memory.",
  evidence: { finding_codes: ["episodic_blobs"], supporting_count: 5, supporting: [CITED], counter_count: 25 },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
  effect: "future",
  historical:
    "Memories already stored keep their shape. Reworking them is the optimize procedure, a separate action with its own approval: each is moved to a new key, and its old key redirects to the new one.",
  refs: [
    "namespace.audit",
    "memory.set_from_messages",
    "memory.set",
    "memory.bulk_set",
    "memory.update_by_id",
    "source_trace",
    "memory.move",
    "loom-optimize",
    "episodic_blobs",
    "context.offload",
  ],
  apply: {
    op: "memory.set_from_messages",
    args: {
      extraction_instructions:
        "- Keep an episode to a short summary; a run's full output belongs in offloaded context, not in memory.",
    },
    args_complete: false,
    required_role: "writer",
    guard: "None needed: the instructions apply only to the request that carries them.",
  },
  limits:
    "Each writer has to send them; a request without them extracts as before. The flagged counts include memories written directly, which never pass through extraction; a memory counts as extraction's by the source it records. They are not drafted as the operator extraction policy: a policy overrides every other instruction and turns off the recovery passes that keep preferences, costs, ages and corrections.",
});

const TUNE: NamespaceTune = {
  org: "dev",
  namespace: "team",
  computed_at: "2026-11-01T10:00:00",
  proposals: [CATEGORY, GUIDE, INSTRUCTIONS],
  guide_revision: 2,
  categories_registered: 1,
  extraction_policy: null,
  extraction_policy_unavailable: "only an org admin reads the operator extraction policy",
  // No live document: nothing sampled.
  chunking: null,
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
  refs: [`ref_of_${code}`],
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

/** namespace.audit's two checks on memories shaped like documents (inspect.py
 *  MEMORY_FINDING_CODES), each sent when it ran, a clean one as `info` at count 0, over the
 *  namespace's 40 searchable rows. The detail and remedy are the audit's. */
const memoryFindings = (oversized: number, episodic: number): KnowledgeInspection["memory_findings"] => [
  {
    code: "oversized_values",
    kind: "usage_heuristic",
    severity: oversized ? "warning" : "info",
    rule_version: 1,
    source_op: "namespace.audit",
    count: oversized,
    share: oversized / 40,
    detail:
      "Searchable rows longer than the memory value advisory (30000 chars): a document, not a memory, and past the embedder's cap it is never embedded at all.",
    remedy:
      "Store the document with `knowledge.upsert` and write a one-paragraph memory row under the same key that points at it. See docs/guides/investigation-agent-memory.md.",
  },
  {
    code: "episodic_blobs",
    kind: "usage_heuristic",
    severity: episodic ? "warning" : "info",
    rule_version: 1,
    source_op: "namespace.audit",
    count: episodic,
    share: episodic / 40,
    detail:
      "Episodic rows longer than 50000 chars: a checkpoint or a run dump stored as a memory row, versioned on every rewrite.",
    remedy:
      "Park it with `context.offload` (a summary plus a TTL) and keep the run ledger as a short episodic row keyed by run id. See docs/guides/investigation-agent-memory.md.",
  },
];

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
    // server/knowledge/app/inspect.py DECISIONS_DEFINITION and _DECISIONS_REFS.
    definition:
      "Live decisions in this namespace that cite a section of one of its documents, as decision extraction and decision recording write them, within what your token reads, by stage: decided, or proposed. Decisions written into another namespace (a repository's) are not counted.",
    refs: [
      "source_resource_id",
      "knowledge:<namespace>/<source_uri>#<section>",
      "loom decisions extract",
      "decision.record",
      "decision:proposed",
    ],
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
  memory_findings: memoryFindings(2, 0),
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
  memory_findings: memoryFindings(0, 0),
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
    ...Array.from({ length: MAX_GROUPS }, (_, i): [string, string, number, number] => [
      `f${i % 25}`,
      `s${Math.floor(i / 25)}`,
      0,
      1,
    ]),
  ]
    // store.document_groups: live DESC, deleted DESC, doc_format, source_scheme.
    .sort(
      (a, b) =>
        b[2] - a[2] || b[3] - a[3] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0),
    )
    .slice(0, MAX_GROUPS);
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
  updated_at: JOB.queued_at,
  // A job knowledge.ingest queues is due at once (store/ingestions.py).
  next_attempt_at: JOB.queued_at,
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
// A failed job keeps the stage it last ran; retries_exhausted used up
// knowledge_ingest_max_attempts (default 5).
type HitJob = Pick<IngestJob, "state" | "stage" | "attempts" | "failure_class" | "error_code">;
const HIT_JOB: Record<string, HitJob> = {
  ingest_backlog: { state: "pending", stage: "", attempts: 0, failure_class: "", error_code: "" },
  extractor_unavailable: {
    state: "failed",
    stage: "parse",
    attempts: 1,
    failure_class: "extractor_unavailable",
    error_code: "format_unavailable",
  },
  ingest_missing_access: {
    state: "failed",
    stage: "parse",
    attempts: 1,
    failure_class: "missing_access",
    error_code: "original_gone",
  },
  ingest_content_rejected: {
    state: "failed",
    stage: "parse",
    attempts: 1,
    failure_class: "bad_content",
    error_code: "corrupt",
  },
  ingest_failed_on_server: {
    state: "failed",
    stage: "embed",
    attempts: 5,
    failure_class: "retries_exhausted",
    error_code: "retries_exhausted",
  },
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
      updated_at: open ? JOB.queued_at : JOB.updated_at,
      ended_at: open ? null : JOB.ended_at,
      next_attempt_at: open ? JOB.queued_at : null,
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

// A signed-in token with these claims; the page reads its scope from them, unverified.
const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
const token = (claims: object) => `${b64({ alg: "HS256" })}.${b64({ sub: "ada", ...claims })}.sig`;
const pinned = (claims: object) => localStorage.setItem("loom_token_v1", token(claims));

const PINNED_INVENTORY: NamespaceInventory = {
  ...INVENTORY,
  scope: { ...INVENTORY.scope, read_scope: "own_user", user_id: "ada", session_id: null },
};

describe("Assistant: overview", () => {
  it("explains itself and shows the overview with no click; inspects and drafts nothing on load", async () => {
    const { container } = render(<Assistant />);
    expect(screen.getByRole("heading", { level: 1, name: "Assistant" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing on this page changes your data/)).toBeInTheDocument();
    await screen.findByTestId("assistant-inventory");
    expect(namespaceInventory).toHaveBeenCalledTimes(1);
    expect(namespaceInventory).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, expect.anything());
    for (const name of ["Inspect memories", "Inspect documents", "Open the graph", "Draft improvements"])
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About Who appears here" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Inspect documents" })).toHaveAccessibleDescription(
      "Every document here: formats, what search serves, and ingest health.",
    );
    expect(screen.queryByRole("heading", { name: /Review changes/ })).toBeNull();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(inspectKnowledge).not.toHaveBeenCalled();
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("offers no read to a caller whose role on the namespace is unknown", async () => {
    useStore.setState({ myNamespaces: [] });
    render(<Assistant />);
    const draft = screen.getByRole("button", { name: "Draft improvements" });
    expect(draft).toHaveAttribute("aria-disabled", "true");
    expect(draft).toHaveAccessibleDescription("Your role on this namespace is not known.");
    await userEvent.click(draft);
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(namespaceInventory).not.toHaveBeenCalled();
    const overview = screen.getByRole("region", { name: "Overview" });
    expect(
      within(overview).getByText("Your role on this namespace is not known.", { selector: "p" }),
    ).toBeInTheDocument();
  });

  it("reads the overview once the role is known, showing a loader rather than a refusal while it loads", async () => {
    useStore.setState({ myNamespaces: null });
    let answer: (rows: MyNamespaceEntry[]) => void = () => {};
    vi.mocked(listMine).mockImplementationOnce(() => new Promise((r) => (answer = r)));
    render(<Assistant />);
    expect(await screen.findByText("Loading the overview")).toBeInTheDocument();
    const overview = screen.getByRole("region", { name: "Overview" });
    expect(within(overview).queryByText("Your role on this namespace is not known.", { selector: "p" })).toBeNull();
    for (const name of ["Refresh", "Inspect memories", "Inspect documents", "Draft improvements"])
      expect(screen.getByRole("button", { name })).toHaveAccessibleDescription(
        "Your role on this namespace is still loading.",
      );
    expect(namespaceInventory).not.toHaveBeenCalled();
    // A button focused while it waits keeps the focus once its read is allowed.
    const draft = screen.getByRole("button", { name: "Draft improvements" });
    draft.focus();
    await act(async () => answer([row("team", "viewer")]));
    await screen.findByTestId("assistant-inventory");
    expect(namespaceInventory).toHaveBeenCalledTimes(1);
    expect(draft).toHaveFocus();
    expect(draft).not.toHaveAttribute("aria-disabled");
  });

  it("says to type an id when the overview lists nobody", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      populations: INVENTORY.populations.map((p) => ({ ...p, total: 0, top: [] })),
    });
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    expect(screen.getByRole("button", { name: "Inspect memories" })).toHaveAccessibleDescription("Type a user id.");
    // Nobody to pick: the heading does not ask for a pick.
    expect(screen.queryByRole("button", { name: "About Who appears here" })).toBeNull();
  });

  it("shows what the namespace holds, with a section it could not count said so", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-inventory");
    const held = screen.getByRole("region", { name: "What this namespace holds" });
    expect(held.querySelector('[data-plane="memories"]')).toHaveTextContent("Memories42");
    expect(within(held).getByTestId("inventory-incomplete")).toHaveTextContent("Could not be read (TimeoutError)");
    expect(screen.getByText(/Model: not enabled/)).toBeInTheDocument();
  });

  it("lists members apart from the users observed on rows, and says when members are incomplete", async () => {
    render(<Assistant />);
    const list = await screen.findByTestId("assistant-populations");
    const items = within(list).getAllByRole("listitem");
    const members = items.find((li) => li.getAttribute("data-population") === "members");
    expect(members).toHaveTextContent(/people with access to this namespace/);
    expect(members).toHaveTextContent("Shared with everyone, a group or an agent: those members are not counted");
    // Users come first: they are what a viewer picks most.
    expect(items[0]).toHaveAttribute("data-population", "subjects");
  });

  it("inspects a user picked in the overview, with no typing, and moves the focus to the answer", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    await userEvent.click(screen.getByRole("button", { name: "ada 12 memories" }));
    expect(inspectMemories).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "user", "ada", expect.anything());
    await screen.findByTestId("assistant-inspection");
    expect(screen.getByRole("heading", { level: 2, name: "Memories" })).toHaveFocus();
    expect(screen.getByLabelText("User id")).toHaveValue("ada");
    // The definitions say it in words; the exact names both views mean are listed once.
    const names = within(screen.getByTestId("assistant-inspection"))
      .getAllByTestId("exact-names")
      .map((e) => e.textContent);
    expect(names.filter((t) => t === "Exact names: user_id, read_scope own_user, memory.promote")).toHaveLength(1);
  });

  it("clears the id on a namespace switch and a change of subject, and the entity on a switch", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    await userEvent.click(screen.getByRole("button", { name: "ada 12 memories" }));
    expect(screen.getByLabelText("User id")).toHaveValue("ada");
    // The subject already selected changes nothing.
    await userEvent.click(screen.getByRole("button", { name: "User" }));
    expect(screen.getByLabelText("User id")).toHaveValue("ada");
    await userEvent.click(screen.getByRole("button", { name: "Session" }));
    expect(screen.getByLabelText("Session id")).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: "ada 12 memories" }));
    await userEvent.type(screen.getByLabelText("Start from (optional)"), "invoices");
    switchNamespace();
    expect(screen.getByLabelText("User id")).toHaveValue("");
    expect(screen.getByLabelText("Start from (optional)")).toHaveValue("");
  });

  it("moves the focus to the answer for a pick made while an earlier one is still read", async () => {
    inspectMemories.mockImplementationOnce(() => new Promise<MemoryInspection>(() => {}));
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    await userEvent.click(screen.getByRole("button", { name: "ada 12 memories" }));
    await userEvent.click(screen.getByRole("button", { name: "claude-code 40 memories" }));
    expect(inspectMemories).toHaveBeenLastCalledWith(expect.anything(), "agent", "claude-code", expect.anything());
    await screen.findByTestId("assistant-inspection");
    expect(screen.getByRole("heading", { level: 2, name: "Memories" })).toHaveFocus();
  });

  it("inspects an agent picked in the overview", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    await userEvent.click(screen.getByRole("button", { name: "claude-code 40 memories" }));
    expect(inspectMemories).toHaveBeenCalledWith(expect.anything(), "agent", "claude-code", expect.anything());
    await screen.findByTestId("assistant-inspection");
    expect(screen.getByRole("heading", { level: 2, name: "Memories" })).toHaveFocus();
    expect(screen.getByLabelText("Agent name")).toHaveValue("claude-code");
  });

  it("labels an agent suggestion and pick as the dashboard names that agent", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      populations: INVENTORY.populations.map((p) =>
        p.population === "agents" ? { ...p, top: [{ name: "-", rows: 5 }] } : p,
      ),
    });
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    expect(screen.getByRole("button", { name: "No agent (admin / me) 5 memories" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Agent" }));
    const option = document.querySelector("#assistant-subject-suggestions option");
    expect([option?.getAttribute("value"), option?.textContent]).toEqual(["-", "No agent (admin / me)"]);
    // The answer is headed by the same name.
    await userEvent.click(screen.getByRole("button", { name: "No agent (admin / me) 5 memories" }));
    expect(await screen.findByTestId("assistant-inspection")).toHaveTextContent("Agent No agent (admin / me)");
  });

  it("names a pick with one memory in the singular", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      populations: INVENTORY.populations.map((p) =>
        p.population === "subjects" ? { ...p, top: [{ name: "ada", rows: 1 }] } : p,
      ),
    });
    render(<Assistant />);
    expect(await screen.findByRole("button", { name: "ada 1 memory" })).toBeInTheDocument();
  });

  it("lists authors without offering to inspect them", async () => {
    render(<Assistant />);
    const list = await screen.findByTestId("assistant-populations");
    const authors = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "authors");
    expect(authors).toHaveTextContent("human:o@example.com42");
    expect(within(authors as HTMLElement).queryByRole("button")).toBeNull();
  });

  it("suggests the users and agents the overview found in the id field", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-populations");
    const field = screen.getByLabelText("User id");
    expect(field).toHaveAttribute("list", "assistant-subject-suggestions");
    const options = () => Array.from(document.querySelectorAll("#assistant-subject-suggestions option"));
    expect(options().map((o) => o.getAttribute("value"))).toEqual(["ada"]);
    await userEvent.click(screen.getByRole("button", { name: "Agent" }));
    expect(options().map((o) => [o.getAttribute("value"), o.textContent])).toEqual([["claude-code", "claude-code"]]);
    await userEvent.click(screen.getByRole("button", { name: "Session" }));
    expect(screen.getByLabelText("Session id")).not.toHaveAttribute("list");
  });

  it("says a population lists only its largest identities", async () => {
    namespaceInventory.mockResolvedValueOnce({
      ...INVENTORY,
      // As the server sends it: the top 20 of 340.
      populations: INVENTORY.populations.map((p) =>
        p.population === "subjects"
          ? { ...p, total: 340, top: Array.from({ length: 20 }, (_, n) => ({ name: `user-${n}`, rows: 40 - n })) }
          : p,
      ),
    });
    render(<Assistant />);
    const list = await screen.findByTestId("assistant-populations");
    const subjects = within(list)
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-population") === "subjects");
    expect(subjects).toHaveTextContent("The 20 with the most memories are shown.");
  });

  it("says the counts are bound to the user the token is pinned to", async () => {
    namespaceInventory.mockResolvedValueOnce(PINNED_INVENTORY);
    render(<Assistant />);
    expect(await screen.findByTestId("inventory-scope")).toHaveTextContent(
      "Memories, runs and the users, agents and authors are counted for user ada only, as your token reads; documents and members are the whole namespace's.",
    );
  });

  it("offers no inspection or drafts once the overview shows a token pinned to one user", async () => {
    namespaceInventory.mockResolvedValueOnce(PINNED_INVENTORY);
    render(<Assistant />);
    await screen.findByTestId("inventory-scope");
    const draft = screen.getByRole("button", { name: "Draft improvements" });
    expect(draft).toHaveAccessibleDescription(/limited to one user or session/);
    await userEvent.click(draft);
    const pick = screen.getByRole("button", { name: "ada 12 memories" });
    expect(pick).toHaveAccessibleDescription(/limited to one user or session/);
    expect(screen.queryByRole("button", { name: "About Who appears here" })).toBeNull();
    await userEvent.click(pick);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(tuneNamespace).not.toHaveBeenCalled();
    expect(inspectMemories).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Refresh" })).not.toHaveAttribute("aria-disabled");
  });

  it("offers no inspection or drafts to a token whose own claim pins it, before any read", async () => {
    pinned({ read_user_id: "ada" });
    try {
      render(<Assistant />);
      const draft = screen.getByRole("button", { name: "Draft improvements" });
      expect(draft).toHaveAccessibleDescription(/limited to one user or session/);
      await userEvent.click(draft);
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
  ])("reads no overview for a token scoped %s, which this page cannot complete", async (_label, claims) => {
    pinned(claims);
    try {
      render(<Assistant />);
      const refresh = screen.getByRole("button", { name: "Refresh" });
      expect(refresh).toHaveAccessibleDescription(
        "Your token needs each request to name its user or session, which this page does not send.",
      );
      await userEvent.click(refresh);
      expect(namespaceInventory).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("reads the overview again for a token signed in again with the namespace's scope", async () => {
    pinned({ read_user_id: "ada" });
    namespaceInventory.mockResolvedValueOnce(PINNED_INVENTORY);
    try {
      render(<Assistant />);
      await screen.findByTestId("inventory-scope");
      localStorage.setItem("loom_token_v1", token({}));
      act(() => {
        useStore.setState({ whoami: { ...viewer } });
      });
      await waitFor(() => expect(screen.queryByTestId("inventory-scope")).toBeNull());
      await screen.findByTestId("assistant-inventory");
      expect(namespaceInventory).toHaveBeenCalledTimes(2);
      expect(screen.getByRole("button", { name: "Draft improvements" })).not.toHaveAttribute("aria-disabled");
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("adds no scope note when the counts cover the namespace", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-inventory");
    expect(screen.queryByTestId("inventory-scope")).toBeNull();
  });

  it("reads again on Refresh, keeping the last answer on screen, marked busy", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-inventory");
    namespaceInventory.mockImplementationOnce(() => new Promise<NamespaceInventory>(() => {}));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(namespaceInventory).toHaveBeenCalledTimes(2);
    const overview = screen.getByTestId("assistant-inventory");
    expect(overview.closest("[aria-busy]")).toHaveAttribute("aria-busy", "true");
  });

  it("says when the overview cannot be read", async () => {
    namespaceInventory.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the overview");
  });

  it("shows nothing of the old namespace's overview on the first render after a switch", async () => {
    namespaceInventory.mockImplementation(async () => INVENTORY);
    const { rerender } = render(<Assistant />);
    await screen.findByTestId("assistant-inventory");
    namespaceInventory.mockImplementationOnce(() => new Promise<NamespaceInventory>(() => {}));
    useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } });
    rerender(<Assistant />);
    expect(screen.queryByTestId("assistant-inventory")).toBeNull();
  });

  it("reads the new namespace's overview when the namespace changes", async () => {
    render(<Assistant />);
    await screen.findByTestId("assistant-inventory");
    switchNamespace();
    await waitFor(() =>
      expect(namespaceInventory).toHaveBeenLastCalledWith({ org: "dev", namespace: "other" }, expect.anything()),
    );
  });
});

describe("Assistant: relationships", () => {
  it("opens the graph with no entity, on its own starting points", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Open the graph" }));
    expect(useStore.getState().view).toBe("graph");
  });

  it("opens the graph from an entity typed in", async () => {
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("Start from (optional)"), "Invoices{Enter}");
    expect(useStore.getState().view).toBe("graph");
    expect(window.location.search).toContain("seed=invoices");
  });
});

describe("Assistant: inspection", () => {
  it("inspects a user: the rows filed under the user, apart from the rest", async () => {
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(inspectMemories).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "user", "ada", expect.anything());
    expect(within(result).getByRole("heading", { name: /Filed under this user/ })).toBeInTheDocument();
    expect(within(result).getByText("ada")).toHaveClass("break-all");
    expect(within(result).getByText(OWN_USER)).toBeInTheDocument();
    expect(
      within(result).getByRole("heading", { name: /Every other live memory of the namespace/ }),
    ).toBeInTheDocument();
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
    expect(within(result).getByText("No live memories.")).toBeInTheDocument();
    expect(within(result).getByTestId("inspect-policy")).toHaveTextContent(
      "not shown (only an org admin reads the operator extraction policy)",
    );
    expect(within(result).getByText("No calls in the last 7 days.")).toBeInTheDocument();
  });

  it.each([
    ["Agent", "Agent name", "agent", "claude-code", "Written by this agent"],
    ["Session", "Session id", "session", "s-1", "Written in this session"],
  ] as const)("inspects a %s with its own heading", async (label, field, subject, id, heading) => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: label }));
    await userEvent.type(screen.getByLabelText(field), id);
    await userEvent.click(screen.getByRole("button", { name: "Inspect memories" }));
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
    expect(within(result).getByText("The memories could not be counted.")).toBeInTheDocument();
    expect(within(result).queryByText("No live memories.")).toBeNull();
  });

  it("states the activity window, and the ledger's retention beside the calls", async () => {
    inspectMemories.mockResolvedValueOnce({
      ...inspection("user", "ada"),
      activity: {
        window_seconds: 30 * 86_400,
        calls: [{ operation: "memory.search", calls: 9, succeeded: 8 }],
        first_at: "2026-10-20T10:00:00",
        last_at: "2026-11-01T10:00:00",
        gap: "Calls are kept for 7 days, so older ones are not counted.",
      },
    });
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    const result = await screen.findByTestId("assistant-inspection");
    expect(within(result).getByText(/Last 30 days, succeeded of called: memory.search 8\/9/)).toBeInTheDocument();
    expect(within(result).getByText("Calls are kept for 7 days, so older ones are not counted.")).toBeInTheDocument();
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
    expect(within(result).getByText("The memories could not be listed.")).toBeInTheDocument();
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
    expect(within(result).getByText(/How long calls are kept could not be read/)).toBeInTheDocument();
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
    expect(within(own).getByText("The memories could not be counted.")).toBeInTheDocument();
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
    const inspect = screen.getByRole("button", { name: "Inspect memories" });
    expect(inspect).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(inspect);
    await userEvent.type(screen.getByLabelText("User id"), "{Enter}");
    expect(inspectMemories).not.toHaveBeenCalled();
  });

  it("says when the inspection cannot be read", async () => {
    inspectMemories.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.type(screen.getByLabelText("User id"), "ada{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the memories");
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
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
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
    expect(within(card).queryByRole("button", { name: "Apply" })).toBeNull();
    expect(within(card).getByTestId("how-to-apply")).toHaveTextContent(
      "How to apply: A writer on this namespace sends the request below.",
    );
    expect(within(card).getByTestId("exact-names")).toHaveTextContent(
      "Exact names: memory.set_from_messages, namespace.search_categories, memory.assign_category, namespace.list_categories",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "audit.telemetry_unavailable: window within the ledger lag",
    );
    expect(tuneNamespace).toHaveBeenCalledTimes(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("renders a draft from a server a release behind, which sends no via, apply_also, preview or documents", async () => {
    const legacy = ({
      apply: { via: _via, ...apply },
      apply_also: _also,
      preview: _preview,
      evidence,
      ...p
    }: TuneProposal) => {
      const { documents: _documents, ...rest } = evidence;
      return { ...p, evidence: rest, apply } as TuneProposal;
    };
    const { chunking: _chunking, ...old } = TUNE;
    tuneNamespace.mockResolvedValueOnce({ ...old, proposals: TUNE.proposals.map(legacy) });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", { name: /Register a category for the tag/ });
    expect(within(card).getByText("namespace.register_category")).toBeInTheDocument();
    expect(within(card).getByText("Arguments:")).toBeInTheDocument();
    expect(within(card).queryByTestId("proposal-also")).toBeNull();
    expect(within(review).queryByTestId("tune-sample")).toBeNull();
  });

  it("shows a guide draft's revision and arguments, and says when the writers could not be read", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByText("Current")).toBeInTheDocument();
    expect(within(review).queryByText(/revision 0/)).toBeNull();
  });

  it("says that each writer sends the extraction instructions itself", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    const card = within(review).getByRole("listitem", {
      name: /Send extraction instructions for the shapes the audit found/,
    });
    expect(within(card).getByText("Each writer adds these arguments to its own requests:")).toBeInTheDocument();
    expect(
      within(card).getByText(/episodic_blobs · 5 supporting · at least 25 unflagged memories it must keep working for/),
    ).toBeInTheDocument();
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"extraction_instructions"');
    expect(within(card).getByTestId("how-to-apply")).toHaveTextContent(/No single request applies this/);
    expect(within(card).getByTestId("affected-agents")).toHaveTextContent("Affected agents:claude-code");
    expect(within(card).getByTestId("exact-names")).toHaveTextContent(
      "Exact names: namespace.audit, memory.set_from_messages, memory.set, memory.bulk_set, memory.update_by_id, source_trace, memory.move, loom-optimize, episodic_blobs, context.offload",
    );
  });

  it("does not claim the evidence supports no change when a read failed", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: ["audit: TimeoutError"] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent("audit: TimeoutError");
  });

  it("lists what was not drafted, and why, when the evidence rules every draft out", async () => {
    const reason =
      "extraction: no live memory here records extraction from messages as its source, so no rule is drafted for episodic blobs (episodic_blobs)";
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [reason] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("status")).toHaveTextContent(
      "No drafts. What was skipped, and why, is listed below.",
    );
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(reason);
  });

  it("says the evidence supports no change when nothing was left out", async () => {
    tuneNamespace.mockResolvedValueOnce({ ...TUNE, proposals: [], skipped: [] });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByRole("listitem", { name: /Register a category for the tag/ })).toBeInTheDocument();
    expect(within(review).getByTestId("tune-skipped")).toHaveTextContent(
      "Skipped:category.billing.cited: TimeoutError",
    );
    expect(within(review).queryByText(/Not drafted/)).toBeNull();
  });

  it("keeps the drafts on screen, marked busy, while they are drafted again", async () => {
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    await screen.findByTestId("assistant-review");
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = screen.getByTestId("assistant-review");
    expect(review.parentElement).toHaveAttribute("aria-busy", "true");
    expect(within(review).getByText("Register a category for the tag `billing`")).toBeInTheDocument();
  });

  it("says when the drafts cannot be read", async () => {
    tuneNamespace.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the drafts");
    expect(screen.queryByRole("heading", { name: /Review changes/ })).toBeNull();
    expect(screen.queryByTestId("assistant-review")).toBeNull();
  });

  it("is not left loading when the page comes back to a namespace it left mid-read", async () => {
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>(() => {}));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    switchNamespace();
    act(() => {
      useStore.setState({ identity: { org: "dev", namespace: "team", agent: "loom-ui" } });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Draft improvements" })).not.toHaveAttribute("aria-busy"),
    );
    expect(screen.queryByText("Loading the drafts")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    expect(await screen.findByTestId("assistant-review")).toBeInTheDocument();
  });

  it("drops a draft that answers after the namespace changed", async () => {
    let answer: (t: NamespaceTune) => void = () => {};
    tuneNamespace.mockImplementationOnce(() => new Promise<NamespaceTune>((r) => (answer = r)));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    switchNamespace();
    await act(async () => answer(TUNE));
    await waitFor(() => expect(screen.queryByText("Loading the drafts")).toBeNull());
    expect(screen.queryByTestId("assistant-review")).toBeNull();
  });
});

const pickDocuments = async () => {
  await userEvent.click(screen.getByRole("button", { name: "Inspect documents" }));
  return screen.findByTestId("assistant-documents");
};

describe("Assistant: documents", () => {
  it("inspects the namespace's documents with no id: formats, what search serves, decisions and the newest", async () => {
    const { container } = render(<Assistant />);
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
    const unembedded = within(result).getByRole("listitem", { name: "Chunks with no vector" });
    expect(unembedded).toHaveTextContent("1 document of 2 (50%)");
    expect(unembedded).not.toHaveTextContent("ingested from a file");
    expect(within(result).queryByText(/checks? did not run/)).toBeNull();
    expect(within(result).getByTestId("docs-decisions-total")).toHaveTextContent(
      "Decided: 3 (1 confirmed, 2 unconfirmed, 0 source changed, 0 with an outcome) · Proposed: none",
    );
    const decisions = within(result).getByRole("region", { name: "Decisions recorded from documents" });
    expect(within(decisions).getByTestId("exact-names")).toHaveTextContent("decision:proposed");
    const spec = result.querySelector('[data-source="https://example.com/spec.pdf"]') as HTMLElement;
    expect(spec).toHaveTextContent("Chunks: 8 of 8 searchable");
    expect(spec).toHaveTextContent("Extraction: 9 of 10 pages extracted, 1 failed, lost section paths");
    expect(spec).toHaveTextContent("job ing-1: partial");
    const notes = result.querySelector('[data-source="https://example.com/notes.md"]') as HTMLElement;
    expect(notes).toHaveTextContent("Extraction: none (a text write, which no extractor read)");
    expect(within(result).getByTestId("docs-extraction")).toHaveTextContent(
      "Extraction coverage: 1 of 1 ingested document not complete · lost: section paths 1",
    );
    expect(
      within(result)
        .getAllByTestId("exact-names")
        .map((e) => e.textContent),
    ).toContain("Exact names: section_path");
    expect(within(result).getByText(/Oversized values/)).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("says an empty namespace holds no document, and lists no document sections", async () => {
    inspectKnowledge.mockResolvedValueOnce(emptyNamespace());
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByRole("status")).toHaveTextContent("This namespace holds no live document");
    expect(within(result).queryByRole("heading", { name: /Newest documents/ })).toBeNull();
    expect(within(result).queryByRole("heading", { name: "What search serves" })).toBeNull();
    expect(within(result).getByText("No ingestion job in the last 30 days.")).toBeInTheDocument();
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
    expect(within(result).getByTestId("docs-skipped")).toHaveTextContent("Skipped: documents: TimeoutError; chunks");
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
      expect(within(card).getByTestId("exact-names")).toHaveTextContent(`Exact names: ref_of_${code}`);
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
    expect(within(result).getByText("The ingestion jobs could not be read.")).toBeInTheDocument();
    // Only the job read failed: its five checks did not run; the coverage check did.
    expect(within(result).getByText("The job checks did not run.")).toBeInTheDocument();
    expect(within(result).queryByText("The extraction coverage check did not run.")).toBeNull();
    expect(within(result).getByText(/Checked, none found: Extraction incomplete/)).toBeInTheDocument();
  });

  it("says the coverage check did not run when only the coverage read failed", async () => {
    inspectKnowledge.mockResolvedValueOnce({
      ...KNOWLEDGE,
      extraction: { documents: null, incomplete: null, lost: [] },
      ingest_findings: KNOWLEDGE.ingest_findings.filter((f) => f.code !== "extraction_incomplete"),
      coverage: {
        rules_run: ALL_CHECKS.filter((c) => c !== "extraction_incomplete"),
        rules_skipped: ["extraction_incomplete"],
        complete: false,
      },
      skipped: ["extraction: TimeoutError"],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByText("The extraction coverage check did not run.")).toBeInTheDocument();
    expect(within(result).queryByText("The job checks did not run.")).toBeNull();
    expect(within(result).getByText(/Checked, none found: Backlog/)).toBeInTheDocument();
    expect(within(result).getByTestId("docs-rules-skipped")).toHaveTextContent("Checks not run: Extraction incomplete");
    // The clean checks' codes are listed apart, like the ones not run.
    const names = within(result)
      .getAllByTestId("exact-names")
      .map((e) => e.textContent ?? "");
    expect(names.some((t) => t.startsWith("Exact names: ingest_backlog"))).toBe(true);
  });

  it("says the sources with no live document were not listed when that read failed", async () => {
    const queued = firstIngestQueued();
    inspectKnowledge.mockResolvedValueOnce({
      ...queued,
      ingestions: { ...queued.ingestions, without_document_listed: [] },
      skipped: ["ingestions.without_document: TimeoutError"],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    expect(within(result).getByTestId("docs-without-document-unlisted")).toHaveTextContent(
      "Sources with a job and no live document (1): not listed.",
    );
    expect(within(result).queryByRole("list", { name: "Sources with a job and no live document" })).toBeNull();
  });

  it("marks the chunk-check samples knowledge.ingest published, whose remedy is to ingest again", async () => {
    // The spec also has a live chunk with no vector: 2 of the 2 live documents, 1 ingested.
    const [spec, notes] = KNOWLEDGE.listed;
    inspectKnowledge.mockResolvedValueOnce({
      ...KNOWLEDGE,
      chunks: {
        ...KNOWLEDGE.chunks,
        searchable: 10,
        unembedded: 2,
        by_model: [{ model: "text-embedding-3-small", live: 12, embedded: 10 }],
      },
      listed: [{ ...spec, chunks: spec.chunks && { ...spec.chunks, searchable: 7, unembedded: 1 } }, notes],
      findings: KNOWLEDGE.findings.map((f) =>
        f.code === "unembedded_chunks"
          ? {
              ...f,
              count: 2,
              share: 1,
              samples: [SPEC, NOTES],
              ingested: 1,
              ingested_samples: [SPEC],
            }
          : f,
      ),
    });
    render(<Assistant />);
    const result = await pickDocuments();
    const card = within(result).getByRole("listitem", { name: "Chunks with no vector" });
    expect(card).toHaveTextContent("2 documents of 2 (100%) · 1 ingested from a file");
    const samples = within(within(card).getByRole("list", { name: /Samples of/ })).getAllByRole("listitem");
    expect(samples[0]).toHaveTextContent(`${SPEC} · ingested from a file`);
    expect(samples[1]).not.toHaveTextContent("ingested from a file");
  });

  it("names the ingested samples the server sampled apart from the first matches", async () => {
    // 22 live documents miss a vector: the notes and the spec, and 20 newer text writes. The
    // server cuts the matches to the first 20 (the default sample_limit), all text writes, and
    // samples the ingested ones apart, so the spec is only in `ingested_samples` and the notes
    // are left unlisted. The listing holds the 20 newest documents.
    const [, notes] = KNOWLEDGE.listed;
    const text = Array.from({ length: 20 }, (_, k) => `https://docs.example.com/note-${k + 1}.md`);
    const rows = text.map((uri, k) => ({
      ...notes,
      source_uri: uri,
      title: `Note ${k + 1}`,
      ingested_at: `2026-11-02T09:${String(59 - k).padStart(2, "0")}:00`,
      chunks: notes.chunks && {
        ...notes.chunks,
        live: 1,
        searchable: 0,
        unembedded: 1,
        min_chars: 300,
        median_chars: 300,
        max_chars: 300,
      },
    }));
    inspectKnowledge.mockResolvedValueOnce({
      ...KNOWLEDGE,
      documents: {
        ...KNOWLEDGE.documents,
        live: 22,
        by_format: [
          { name: "markdown", live: 21, deleted: 1 },
          { name: "pdf", live: 1, deleted: 0 },
        ],
        by_source_scheme: [{ name: "https", live: 22, deleted: 1 }],
        newest_ingested_at: rows[0].ingested_at,
      },
      // The spec's 8 chunks (one now unembedded), the notes' 4 and one per added note.
      chunks: {
        ...KNOWLEDGE.chunks,
        live: 32,
        searchable: 10,
        unembedded: 22,
        by_model: [{ model: "text-embedding-3-small", live: 32, embedded: 10 }],
      },
      listed: rows,
      listed_complete: false,
      findings: KNOWLEDGE.findings.map((f) =>
        f.code === "unembedded_chunks"
          ? {
              ...f,
              count: 22,
              share: 1,
              population: 22,
              samples: text,
              samples_complete: false,
              ingested: 1,
              ingested_samples: [SPEC],
            }
          : { ...f, population: 22, share: f.count / 22 },
      ),
    });
    render(<Assistant />);
    const result = await pickDocuments();
    const card = within(result).getByRole("listitem", { name: "Chunks with no vector" });
    expect(card).toHaveTextContent(`Also ingested from a file: ${SPEC}`);
    expect(card).toHaveTextContent("and 1 more not listed");
  });

  it("shows a share under one percent as <1%, not 0%", async () => {
    // One queued first ingest beside 249 sources whose published document was deleted after
    // its job ended (replaced): the backlog check's share is 1 of 250.
    const queued = firstIngestQueued();
    inspectKnowledge.mockResolvedValueOnce({
      ...queued,
      documents: {
        ...queued.documents,
        deleted: 249,
        by_format: [{ name: "markdown", live: 0, deleted: 249 }],
        by_source_scheme: [{ name: "https", live: 0, deleted: 249 }],
        retired_versions: 249,
      },
      ingestions: { ...queued.ingestions, sources: 250, ready: 249, replaced: 249 },
      ingest_findings: queued.ingest_findings.map((f) =>
        f.code === "extraction_incomplete" ? f : { ...f, population: 250, share: f.count / 250 },
      ),
    });
    render(<Assistant />);
    const result = await pickDocuments();
    const card = within(result).getByRole("listitem", { name: "Backlog" });
    expect(card).toHaveTextContent("1 source of 250 (<1%)");
  });

  it("shows a share just under the whole as >99%, not 100%", async () => {
    // 199 of 200 sources with a job have their first ingest queued; the other published a
    // document that was deleted after its job ended (replaced). The server lists and samples
    // the first 20 of the 199.
    const queued = firstIngestQueued();
    const uris = Array.from({ length: 20 }, (_, k) => `https://example.com/queued-${k + 1}.pdf`);
    const jobs = uris.map((uri, k) => ({ uri, job: { ...PENDING, ingestion_id: `ing-q${k + 1}` } }));
    inspectKnowledge.mockResolvedValueOnce({
      ...queued,
      documents: {
        ...queued.documents,
        deleted: 1,
        by_format: [{ name: "markdown", live: 0, deleted: 1 }],
        by_source_scheme: [{ name: "https", live: 0, deleted: 1 }],
        retired_versions: 1,
      },
      ingestions: {
        ...queued.ingestions,
        sources: 200,
        without_document: 199,
        pending: 199,
        ready: 1,
        replaced: 1,
        without_document_listed: jobs.map((j) => sourceJobs(j.uri, j.job)),
      },
      ingest_findings: queued.ingest_findings.map((f) =>
        f.code === "ingest_backlog"
          ? {
              ...ingestFinding(
                f.code,
                f.kind,
                200,
                jobs.map((j): [string, string] => [j.uri, j.job.ingestion_id]),
                199,
              ),
              count: 199,
              share: 199 / 200,
              samples_complete: false,
            }
          : f.code === "extraction_incomplete"
            ? f
            : { ...f, population: 200 },
      ),
    });
    render(<Assistant />);
    const result = await pickDocuments();
    const card = within(result).getByRole("listitem", { name: "Backlog" });
    expect(card).toHaveTextContent("199 sources of 200 (>99%)");
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
    expect(within(result).getByTestId("docs-skipped")).not.toHaveTextContent("Not read");
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
    expect(within(result).getByText("The extraction coverage check did not run.")).toBeInTheDocument();
    expect(within(result).getByText("The extraction coverage could not be read.")).toBeInTheDocument();
    expect(within(result).queryByTestId("docs-extraction-note")).toBeNull();
  });

  it("lists the audit's memory checks it read, beside the one it names as skipped", async () => {
    const reason = "memory_findings.episodic_blobs: not reported by the namespace audit";
    inspectKnowledge.mockResolvedValueOnce({
      ...KNOWLEDGE,
      memory_findings: KNOWLEDGE.memory_findings.filter((f) => f.code === "oversized_values"),
      skipped: [reason],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    const list = within(result).getByRole("list", { name: "Memory check findings" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list).toHaveTextContent("Oversized values 2 rows");
    expect(within(result).getByTestId("docs-skipped")).toHaveTextContent(reason);
  });

  it("says the audit's memory checks were not read for a token limited to one user", async () => {
    inspectKnowledge.mockResolvedValueOnce({
      ...KNOWLEDGE,
      memory_findings: [],
      skipped: [
        "memory_findings: the namespace audit reads every user's memories, so it does not run for a token limited to one user or session",
      ],
    });
    render(<Assistant />);
    const result = await pickDocuments();
    expect(
      within(result).getByText("The memory checks were not read; the reason is listed below."),
    ).toBeInTheDocument();
    expect(within(result).queryByRole("list", { name: "Memory check findings" })).toBeNull();
  });

  it("offers documents to a token pinned to one user, which the server answers in scope", async () => {
    pinned({ read_user_id: "ada" });
    try {
      render(<Assistant />);
      await pickDocuments();
      expect(inspectKnowledge).toHaveBeenCalledTimes(1);
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("offers no documents to a token scoped own_session, which this page cannot complete", async () => {
    pinned({ read_scope: "own_session", read_user_id: "ada" });
    try {
      render(<Assistant />);
      const inspect = screen.getByRole("button", { name: "Inspect documents" });
      expect(inspect).toHaveAccessibleDescription(
        "Your token needs each request to name its user or session, which this page does not send.",
      );
      await userEvent.click(inspect);
      expect(inspectKnowledge).not.toHaveBeenCalled();
    } finally {
      localStorage.removeItem("loom_token_v1");
    }
  });

  it("says when the documents cannot be read", async () => {
    inspectKnowledge.mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", message: "boom" }));
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Inspect documents" }));
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

// server/knowledge/app/tune.py, word for word where the draft carries the server's text.
const TUNE_SCOPE = "every namespace of this deployment (a deployment setting)";
const SETTING_GUARD =
  "The value it was drafted against: the change is refused when the value in force is no longer that one.";
// Sent whenever the namespace has a live document or a chunk sample (tune.py DECISIONS_SKIPPED).
const DECISIONS_SKIPPED =
  "decision_extraction: no instruction is drafted: decision extraction and a source sync's decision mining build their prompt from fixed text, and no request, namespace or org setting carries instructions for them";
const SAMPLED = Array.from({ length: 30 }, (_, i) => `https://example.com/handbook/${String(i).padStart(2, "0")}.md`);

// The sections-cut rule: 30 sampled documents, 18 of 40 sections cut at size 1000 (more
// than a quarter), and 1500 still cutting too many, so 2000 is drafted with the stride's
// ratio kept. 12 documents have a cut section; the draft cites the first sample_limit (5).
const CHUNKING: TuneProposal = {
  id: "9c0d3a71e5b24f68",
  target: "settings.knowledge_chunking",
  scope: TUNE_SCOPE,
  title: "Cut documents into chunks of 2000 characters",
  rationale:
    "Of 30 documents of dev/team cut again at the current values, the size cuts 18 of 40 sampled sections into more chunks than their structure makes, so a search hit returns part of a section. At size 2000 and stride 1600 the same sample makes 64 chunks (now 120), cuts 2 of 40 sections (now 18) and its largest embedding input is 560 tokens (now 300). The stride keeps its ratio to the size.",
  current: "knowledge_chunk_size=1000, knowledge_chunk_stride=800",
  proposed: "knowledge_chunk_size=2000, knowledge_chunk_stride=1600",
  current_revision: null,
  evidence: {
    finding_codes: [],
    supporting_count: 12,
    supporting: [],
    counter_count: 18,
    documents: SAMPLED.slice(0, 5),
  },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
  effect: "future",
  historical:
    "Documents already stored keep their chunks. A text document is cut again by the rechunk command; an ingested one only by downloading its original, uploading it again and ingesting it (the rechunk command would turn it into a text document). Each is a separate action with its own approval.",
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
    args: { name: "knowledge_chunk_size", value: 2000, expected: 1000 },
    args_complete: true,
    required_role: "platform_admin",
    guard: SETTING_GUARD,
  },
  apply_also: [
    {
      op: "settings.set",
      via: "admin_route",
      args: { name: "knowledge_chunk_stride", value: 1600, expected: 800 },
      args_complete: true,
      required_role: "platform_admin",
      guard: SETTING_GUARD,
    },
  ],
  preview: { before: STATS(1000, 800, 120, 18, 300), after: STATS(2000, 1600, 64, 2, 560) },
  limits:
    "The settings are the deployment's: every namespace's next writes are cut at them, and the evidence is this namespace's sample. A longer chunk keeps more of a section in one vector and a shorter one less; what either does to search quality is not measured here, which a paired search benchmark shows. Until both settings are set, a write uses a stride of at most the size.",
};

const ROUTE_SERVICE = 'LOOM_DOCUMENT_EXTRACTORS_JSON["docx"]';
const ROUTES_PROPOSED = { docx: "<as set>", pdf: `<the URL ${ROUTE_SERVICE} names>` };

// _routes: two pdf sources failed (format_unavailable) with no extractor configured for pdf
// (no default URL, no pdf route); the service a docx route names declares pdf. Five pdf
// documents were extracted whole before, by a route since removed.
const ROUTES: TuneProposal = {
  id: "4f1e8b2c7a903d56",
  target: "settings.document_extractors_json",
  scope: TUNE_SCOPE,
  title: "Route pdf to an extractor that declares it",
  rationale: `pdf: 2 sources failed because no extractor took the format; no extractor is configured for it; the service ${ROUTE_SERVICE} names declares pdf. Counts are dev/team's.`,
  current: '{"docx": "<as set>"}',
  current_revision: null,
  // Python's json.dumps: ", " and ": " between items.
  proposed: `{"docx": "<as set>", "pdf": "<the URL LOOM_DOCUMENT_EXTRACTORS_JSON[\\"docx\\"] names>"}`,
  evidence: {
    finding_codes: ["extractor_unavailable"],
    supporting_count: 2,
    supporting: [],
    counter_count: 5,
    documents: ["https://example.com/a.pdf", "https://example.com/b.pdf"],
  },
  affected_agents: ["claude-code"],
  affected_agents_total: 1,
  effect: "future",
  historical:
    "Sources already ingested keep their extraction, and a failed job does not run again: each is extracted again only by uploading its original again and ingesting it, a separate action with its own approval.",
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
    args: { LOOM_DOCUMENT_EXTRACTORS_JSON: ROUTES_PROPOSED },
    args_complete: false,
    required_role: "platform_admin",
    guard:
      "None: an environment setting has no revision; compare the deployment's value with the current one shown here before setting it. It is in force once each server and worker restarts.",
  },
  apply_also: [],
  preview: null,
  limits:
    "A URL is not shown, as an extractor URL can carry a credential: each placeholder names the setting whose URL it is, and `<as set>` keeps a route as the deployment has it. A service's capabilities name the formats it takes, not what it keeps: ingest one listed source through the new route and inspect the documents' coverage before relying on it. A format's own route wins over a media-type route (`application/pdf`), so the drafted route also takes the sources a media-type route sends elsewhere now.",
};

// namespace_tune_document_sample (default 50), MAX_SAMPLE_CHARS (8 Mi).
const SAMPLE = {
  cap: 50,
  population: 30,
  documents: 30,
  unread: 0,
  load_cap: 8 * 1024 * 1024,
  embedder_model: "text-embedding-3-small",
  input_token_limit: 8191,
  current: STATS(1000, 800, 120, 18, 300),
};

describe("Assistant: document drafts", () => {
  it("shows a chunking draft's before and after on the sample, its cap, and the admin route that applies it", async () => {
    tuneNamespace.mockResolvedValueOnce({
      ...TUNE,
      proposals: [CHUNKING],
      chunking: SAMPLE,
      skipped: [...TUNE.skipped, DECISIONS_SKIPPED],
    });
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    expect(within(review).getByTestId("tune-sample")).toHaveTextContent(
      "Document sample: 30 of 30 live documents re-chunked at the current values (cap 50",
    );
    expect(within(review).getByTestId("tune-sample")).toHaveTextContent("input limit 8,191 tokens");
    const card = within(review).getByRole("listitem", { name: /Cut documents into chunks of 2000/ });
    expect(within(card).getByText("Chunk size and stride (deployment setting)", { exact: false })).toBeInTheDocument();
    const preview = within(card).getByRole("table", { name: /Before and after/ });
    const cut = within(preview).getByText("Sections cut by size").closest("tr") as HTMLElement;
    expect(cut).toHaveTextContent("18 of 40");
    expect(cut).toHaveTextContent("2 of 40");
    expect(within(card).getByText(/12 supporting · 18 sampled documents it does not flag/)).toBeInTheDocument();
    const cited = within(card).getByRole("list", { name: /Documents cited for/ });
    expect(within(cited).getAllByRole("listitem")).toHaveLength(5);
    expect(cited).toHaveTextContent(SAMPLED[4]);
    expect(within(card).getAllByText("POST /v1/admin/settings.set")).toHaveLength(2);
    expect(within(card).getByTestId("proposal-args")).toHaveTextContent('"name": "knowledge_chunk_size"');
    expect(within(card).getByTestId("proposal-also-args")).toHaveTextContent('"name": "knowledge_chunk_stride"');
    expect(within(card).getByText(/Applies to documents written after the change/)).toBeInTheDocument();
    expect(within(card).getByTestId("how-to-apply")).toHaveTextContent(
      "How to apply: A platform admin sends the admin request below.",
    );
    expect(tuneNamespace).toHaveBeenCalledTimes(1);
    expect(within(review).queryByRole("table", { name: "The document sample at the current values" })).toBeNull();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows the sample's own measures when the sample drafts no chunk size", async () => {
    // 6 of 40 sections cut is at most a quarter, so no rule drafts a size and no line is skipped.
    tuneNamespace.mockResolvedValueOnce({
      ...TUNE,
      proposals: [],
      chunking: { ...SAMPLE, current: STATS(1000, 800, 120, 6, 300) },
      skipped: [...TUNE.skipped, DECISIONS_SKIPPED],
    });
    const { container } = render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
    const review = await screen.findByTestId("assistant-review");
    const stats = within(review).getByRole("table", { name: "The document sample at the current values" });
    expect(within(stats).getByText("Sections cut by size").closest("tr")).toHaveTextContent("6 of 40");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("shows an extractor route set in the environment, with its placeholders to fill in and the restart", async () => {
    // The namespace's documents were not sampled (read failed), so no chunking sample.
    tuneNamespace.mockResolvedValueOnce({
      ...TUNE,
      proposals: [ROUTES],
      chunking: null,
      skipped: [...TUNE.skipped, "chunking: TimeoutError", DECISIONS_SKIPPED],
    });
    render(<Assistant />);
    await userEvent.click(screen.getByRole("button", { name: "Draft improvements" }));
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
    expect(within(card).getByTestId("how-to-apply")).toHaveTextContent(
      "How to apply: A platform admin sets the variable below in the deployment; it takes effect once each server and worker restarts.",
    );
    expect(within(card).queryByTestId("proposal-also")).toBeNull();
    expect(within(card).getByTestId("exact-names")).toHaveTextContent(
      "Exact names: format_unavailable, LOOM_DOCUMENT_EXTRACTORS_JSON, current, POST /v1/knowledge.upload, knowledge.ingest, knowledge.inspect",
    );
  });
});
