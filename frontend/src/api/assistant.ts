// The Assistant page's reads: namespace.inventory, memory.inspect, knowledge.inspect and
// namespace.tune. All four are VIEWER, read-only and call no model; the types mirror their
// server models.
import { post } from "../api";
import type { RequestOpts } from "../api";

type Scope = { org: string; namespace: string };

// ===== namespace.inventory (server/identity/api/models_inventory.py) =====

export type InventoryPlane = "memories" | "records" | "documents" | "runs" | "integrations";
export type InventoryPopulationName = "members" | "agents" | "authors" | "subjects";

export interface InventorySection {
  plane: InventoryPlane;
  /** null when the section was not counted; `reason` says why. */
  count: number | null;
  latest_at: string | null;
  complete: boolean;
  reason: string;
}

export interface InventoryPopulation {
  population: InventoryPopulationName;
  total: number | null;
  top: Array<{ name: string; rows: number }>;
  complete: boolean;
  reason: string;
}

export interface NamespaceInventory {
  scope: {
    org: string;
    namespace: string;
    read_scope: "namespace" | "own_user" | "own_session";
    user_id: string | null;
    session_id: string | null;
  };
  sections: InventorySection[];
  populations: InventoryPopulation[];
  capabilities: Array<{ name: "llm" | "embedder"; enabled: boolean }>;
  computed_at: string;
}

export async function namespaceInventory(scope: Scope, opts: RequestOpts = {}): Promise<NamespaceInventory> {
  return post<NamespaceInventory>(
    "/v1/namespace.inventory",
    { org: scope.org, namespace: scope.namespace },
    opts.signal,
  );
}

// ===== memory.inspect (server/memory/api/models_inspect.py) =====

export type InspectSubject = "user" | "agent" | "session";

export interface InspectCount {
  name: string;
  rows: number;
}

export interface InspectRow {
  memory_key: string;
  memory_id: string;
  version: number;
  /** Empty when no hash is served for the row (a high-sensitivity one, a large value). */
  content_sha256: string;
  memory_type: string;
  status: string;
  source_class: string;
  agent: string;
  user_id: string;
  session_id: string;
  actor: string;
  updated_at: string;
  sensitivity: string;
  versions: number;
  excerpt: string;
}

export interface InspectView {
  definition: string;
  /** The exact column, read scope and op the definition names in words. */
  refs?: string[];
  live_rows: number;
  by_type: InspectCount[];
  by_status: InspectCount[];
  by_source_class: InspectCount[];
  top_tags: InspectCount[];
  categories: InspectCount[];
  first_updated_at: string | null;
  last_updated_at: string | null;
  newest: InspectRow[];
  complete: boolean;
}

export interface MemoryInspection {
  org: string;
  namespace: string;
  subject: InspectSubject;
  id: string;
  computed_at: string;
  /** The rows the subject wrote (or that are filed under it). */
  own: InspectView;
  /** Every other live row of the namespace; `definition` says who reads them. */
  others: InspectView;
  activity: {
    window_seconds: number;
    calls: Array<{ operation: string; calls: number; succeeded: number }>;
    first_at: string | null;
    last_at: string | null;
    gap: string;
  };
  settings: {
    guide_revision: number;
    guide_summary: string;
    categories_registered: number;
    extraction_policy: string | null;
    extraction_policy_unavailable: string;
    extraction_instructions: string;
    /** The exact op and argument `extraction_instructions` names in words. */
    extraction_instructions_refs?: string[];
  };
  skipped: string[];
}

export async function inspectMemories(
  scope: Scope,
  subject: InspectSubject,
  id: string,
  opts: RequestOpts = {},
): Promise<MemoryInspection> {
  return post<MemoryInspection>(
    "/v1/memory.inspect",
    { org: scope.org, namespace: scope.namespace, subject, id },
    opts.signal,
  );
}

// ===== namespace.tune (server/identity/api/models_namespace_tune.py) =====

export type TuneTarget =
  | "extraction_instructions.request"
  | "namespace.category"
  | "namespace.guide"
  | "settings.knowledge_chunking"
  | "settings.document_extractors_json";

/** A cited row (server/identity/api/models_findings.py::FindingEvidence). */
export interface TuneEvidenceRef {
  memory_key: string;
  memory_id: string;
  version: number;
  /** Empty when no hash is served for the row (a high-sensitivity one). */
  content_sha256: string;
  agent: string;
  user_id: string;
  session_id: string;
  memory_type: string;
  source_class: string;
  updated_at: string;
}

export interface TuneProposal {
  id: string;
  target: TuneTarget;
  scope: string;
  title: string;
  rationale: string;
  current: string;
  current_revision: number | null;
  proposed: string;
  evidence: {
    finding_codes: string[];
    supporting_count: number;
    supporting: TuneEvidenceRef[];
    /** Rows the change must keep working for: for extraction instructions at least this
     *  many rows the drafted checks did not flag, for a category the tagged rows another
     *  category already holds; for a chunking draft the sampled documents it does not flag,
     *  for an extractor route the documents of its formats extracted whole; null when not
     *  read, or for a guide. */
    counter_count: number | null;
    /** For a document draft, the source_uri of documents or sources that show the problem,
     *  at most the sample limit; `supporting_count` counts them all. */
    documents?: string[];
  };
  affected_agents: string[];
  /** null when the namespace's writers could not be read. */
  affected_agents_total: number | null;
  effect: "future";
  historical: string;
  apply: TuneApply;
  /** Further writes the change needs, in order after `apply`. */
  apply_also?: TuneApply[];
  /** The deterministic before/after a chunking draft rests on; null for other drafts. */
  preview?: { before: ChunkStats; after: ChunkStats } | null;
  limits: string;
  /** Each exact name the text says in words (an op, setting, command, code or field). */
  refs?: string[];
}

/** The ordinary write that would apply a proposal (server: TuneApply). */
export interface TuneApply {
  /** A Loom op; for `via` admin_route the admin endpoint (`settings.set` is
   *  POST /v1/admin/settings.set); for `environment` the variable. */
  op: string;
  /** Absent from a server a release behind, which drafts only ops. */
  via?: "op" | "admin_route" | "environment";
  args: Record<string, unknown>;
  /** False when `args` is not the whole call: for the extraction-instructions draft each
   *  writer merges it into its own memory.set_from_messages requests; for an extractor
   *  route each placeholder is filled in with the URL its setting names. */
  args_complete: boolean;
  required_role: string;
  guard: string;
}

/** A document sample cut at one chunk size and stride (server: ChunkStats). */
export interface ChunkStats {
  size: number;
  stride: number;
  chunks: number;
  min_chars: number;
  median_chars: number;
  p90_chars: number;
  max_chars: number;
  sections: number;
  sections_cut: number;
  max_input_tokens: number;
  /** null when the embedder's input limit is not known. */
  over_input_limit?: number | null;
}

/** The namespace's documents re-chunked in process at the current values (server:
 *  ChunkingSample): no model, nothing written, no text returned. */
export interface ChunkingSample {
  cap: number;
  population: number | null;
  documents: number;
  unread: number;
  load_cap: number;
  embedder_model: string;
  input_token_limit?: number | null;
  current: ChunkStats;
}

export interface NamespaceTune {
  org: string;
  namespace: string;
  computed_at: string;
  proposals: TuneProposal[];
  guide_revision: number | null;
  categories_registered: number | null;
  extraction_policy: string | null;
  extraction_policy_unavailable: string;
  /** null when the namespace has no live document or the sample could not be read;
   *  absent from a server a release behind. */
  chunking?: ChunkingSample | null;
  skipped: string[];
  model_used: boolean;
}

export async function tuneNamespace(scope: Scope, opts: RequestOpts = {}): Promise<NamespaceTune> {
  return post<NamespaceTune>("/v1/namespace.tune", { org: scope.org, namespace: scope.namespace }, opts.signal);
}

// ===== knowledge.inspect (server/knowledge/api/models_inspect.py) =====

export type FindingKind = "contract_violation" | "usage_heuristic" | "system_state";

export interface DocumentCount {
  name: string;
  live: number;
  deleted: number;
}

export interface DocumentChunks {
  live: number;
  searchable: number;
  unembedded: number;
  other_model: number;
  versions: number;
  min_chars: number;
  median_chars: number;
  max_chars: number;
  models: string[];
}

export interface DecisionStage {
  total: number;
  confirmed: number;
  unconfirmed: number;
  source_changed: number;
  outcome_known: number;
}

export interface DocumentDecisions {
  decided: DecisionStage;
  proposed: DecisionStage;
}

export interface ExtractionCoverage {
  unit: string;
  total: number;
  extracted: number;
  failed: number;
  lost: string[];
  complete: boolean;
}

export interface IngestJob {
  ingestion_id: string;
  state: string;
  stage: string;
  attempts: number;
  error_code: string;
  failure_class: string;
  queued_at: string;
  updated_at: string;
  ended_at: string | null;
  next_attempt_at: string | null;
  stalled: boolean;
}

export interface SourceJobs {
  source_uri: string;
  jobs: number;
  open_jobs: number;
  open_ingestion_id: string;
  latest: IngestJob;
  current: IngestJob;
  replaced: boolean;
}

export interface InspectDocument {
  source_uri: string;
  title: string;
  format: string;
  source_scheme: string;
  version: number;
  revision: number;
  ingestion_id: string;
  ingested_at: string;
  /** null when the read failed: not none. */
  retired_versions: number | null;
  chunks: DocumentChunks | null;
  decisions: DocumentDecisions | null;
  /** null for a text write, which no extractor read. */
  extraction: ExtractionCoverage | null;
  jobs: SourceJobs | null;
}

interface FindingFields {
  code: string;
  kind: FindingKind;
  severity: "info" | "warning";
  rule_version: number;
  source_op: string;
  count: number;
  share: number;
  population: number | null;
  sample_limit: number;
  samples: string[];
  samples_complete: boolean;
  detail: string;
  remedy: string;
  /** Each exact name the text says in words (an op, setting, command, code or field). */
  refs?: string[];
  exceptions: string;
  uncertainty: string;
}

export interface DocumentFinding extends FindingFields {
  ingested: number;
  ingested_samples: string[];
}

export interface IngestFinding extends FindingFields {
  ingestion_ids: string[];
  without_document: number;
}

export interface MemoryFindingRef {
  code: string;
  kind: string;
  severity: string;
  rule_version: number;
  source_op: string;
  count: number;
  share: number;
  detail: string;
  remedy: string;
}

/** A section the server could not read is null (or empty) and named in `skipped`. */
export interface KnowledgeInspection {
  org: string;
  namespace: string;
  computed_at: string;
  documents: {
    live: number | null;
    deleted: number | null;
    newest_ingested_at: string | null;
    by_format: DocumentCount[];
    by_source_scheme: DocumentCount[];
    earlier_revisions: number | null;
    unnumbered: number | null;
    retired_versions: number | null;
  };
  chunks: {
    embedder_model: string;
    live: number | null;
    searchable: number | null;
    unembedded: number | null;
    by_model: Array<{ model: string; live: number; embedded: number }>;
  };
  /** `refs`: the exact field, citation format, command, op and tag the definition names. */
  decisions: { definition: string; refs?: string[]; all_documents: DocumentDecisions | null };
  listed: InspectDocument[];
  listed_complete: boolean;
  findings: DocumentFinding[];
  coverage: { rules_run: string[]; rules_skipped: string[]; complete: boolean };
  ingestions: {
    sources: number | null;
    without_document: number | null;
    replaced: number | null;
    pending: number | null;
    processing: number | null;
    stalled: number | null;
    ready: number | null;
    partial: number | null;
    failed: number | null;
    cancelled: number | null;
    oldest_open_queued_at: string | null;
    worker_enabled: boolean;
    extractor_configured: boolean | null;
    without_document_listed: SourceJobs[];
  };
  extraction: { documents: number | null; incomplete: number | null; lost: DocumentCount[] };
  ingest_findings: IngestFinding[];
  memory_findings: MemoryFindingRef[];
  skipped: string[];
}

export async function inspectKnowledge(scope: Scope, opts: RequestOpts = {}): Promise<KnowledgeInspection> {
  return post<KnowledgeInspection>(
    "/v1/knowledge.inspect",
    { org: scope.org, namespace: scope.namespace },
    opts.signal,
  );
}
