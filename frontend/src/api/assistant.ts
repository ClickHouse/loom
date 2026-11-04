// The Assistant page's reads: namespace.inventory, memory.inspect and namespace.tune. All
// three are VIEWER, read-only and call no model; the types mirror their server models.
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

export type TuneTarget = "extraction_instructions.request" | "namespace.category" | "namespace.guide";

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
     *  category already holds; null when not read, or for a guide. */
    counter_count: number | null;
  };
  affected_agents: string[];
  /** null when the namespace's writers could not be read. */
  affected_agents_total: number | null;
  effect: "future";
  historical: string;
  apply: {
    op: string;
    args: Record<string, unknown>;
    /** False for the extraction-instructions draft: no single call applies it; each
     *  writer merges `args` into its own memory.set_from_messages requests. */
    args_complete: boolean;
    required_role: string;
    guard: string;
  };
  limits: string;
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
  skipped: string[];
  model_used: boolean;
}

export async function tuneNamespace(scope: Scope, opts: RequestOpts = {}): Promise<NamespaceTune> {
  return post<NamespaceTune>("/v1/namespace.tune", { org: scope.org, namespace: scope.namespace }, opts.signal);
}
