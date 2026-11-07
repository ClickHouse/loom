// The Review queue's duplicate, proposal and revision reads and decisions.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;

// ===== Duplicate memories (the dedup worker's pairs) =====

/** Mirrors `server/memory/api/models_graph.py::_DedupCluster`. */
export interface MemoryDuplicateCluster {
  canonical_id: string;
  canonical_key: string;
  canonical_agent: string;
  dup_count: number;
  duplicates: { memory_id: string; memory_key: string; agent: string; created_at: string | null }[];
}

export async function listMemoryDuplicates(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ clusters: MemoryDuplicateCluster[] }> {
  return post("/v1/memory.dedup_list", { org: id.org, namespace: id.namespace, limit: 50 }, opts.signal);
}

export type MemoryDuplicateDecision = "keep_canonical" | "keep_duplicate" | "keep_both";

/** POST /v1/memory.dedup_resolve: settles one pair; the server asks for the canonical key
 *  again as its confirmation. */
export async function resolveMemoryDuplicate(
  id: Scope,
  cluster: MemoryDuplicateCluster,
  duplicateId: string,
  decision: MemoryDuplicateDecision,
): Promise<{ canonical_tombstoned: boolean; duplicate_tombstoned: boolean }> {
  return post("/v1/memory.dedup_resolve", {
    org: id.org,
    namespace: id.namespace,
    canonical_id: cluster.canonical_id,
    duplicate_id: duplicateId,
    decision,
    confirm_canonical_key: cluster.canonical_key,
  });
}

/** Mirrors `server/memory/api/models_graph.py::MemoryDedupCollapseOutput`. */
export interface DuplicateCollapse {
  pairs: number;
  restamped: number;
  truncated: boolean;
  sample: { duplicate_key: string; canonical_key: string }[];
}

/** POST /v1/memory.dedup_collapse: every duplicate pair onto its canonical, no model call
 *  and nothing tombstoned. `dryRun` counts what a run would do. */
export async function collapseMemoryDuplicates(id: Scope, dryRun: boolean): Promise<DuplicateCollapse> {
  return post("/v1/memory.dedup_collapse", { org: id.org, namespace: id.namespace, dry_run: dryRun });
}

/** POST /v1/memory.resolve_orphan_duplicates: duplicates whose canonical is gone come back. */
export async function repairOrphanDuplicates(
  id: Scope,
  dryRun: boolean,
): Promise<{ candidates: number; reactivated: number }> {
  return post("/v1/memory.resolve_orphan_duplicates", { org: id.org, namespace: id.namespace, dry_run: dryRun });
}

/** Mirrors `server/memory/api/models_admin.py::MemoryConsolidateResult`. */
export interface ConsolidatedCluster {
  canonical_key: string;
  canonical_value: string;
  merged_keys: string[];
  cluster_size: number;
}

/** POST /v1/memory.consolidate: one model call per cluster of near-duplicate memories of
 *  one agent, merged into one each. `dryRun` finds the clusters and merges none. */
export async function consolidateMemories(
  id: Scope,
  agent: string,
  dryRun: boolean,
): Promise<{ consolidated: ConsolidatedCluster[]; examined: number; clusters_found: number }> {
  return post("/v1/memory.consolidate", { org: id.org, namespace: id.namespace, agent, dry_run: dryRun });
}

/** POST /v1/memory.agents_list: the agents that wrote into the namespace. */
export async function listWritingAgents(id: Scope, opts: RequestOpts = {}): Promise<string[]> {
  const r = await post<{ agents: string[] }>(
    "/v1/memory.agents_list",
    { org: id.org, namespace: id.namespace, agent: "*" },
    opts.signal,
  );
  return r.agents ?? [];
}

// ===== Duplicate documents =====

export interface DocumentDuplicateCluster {
  content_sha256: string;
  doc_count: number;
  docs: { doc_id: string; source_uri: string; title: string; ingested_at: string }[];
}

export async function listDocumentDuplicates(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ doc_clusters: DocumentDuplicateCluster[] }> {
  return post("/v1/knowledge.dedup_list", { org: id.org, namespace: id.namespace, limit: 50 }, opts.signal);
}

/** POST /v1/knowledge.dedup_resolve: `duplicateUri` is superseded by `canonicalUri`. */
export async function retireDocumentDuplicate(id: Scope, canonicalUri: string, duplicateUri: string): Promise<unknown> {
  return post("/v1/knowledge.dedup_resolve", {
    org: id.org,
    namespace: id.namespace,
    canonical_uri: canonicalUri,
    duplicate_uri: duplicateUri,
    decision: "keep_canonical",
    confirm_duplicate_uri: duplicateUri,
  });
}

// ===== Proposals (facts on trial) =====

/** Mirrors `server/memory/api/models_proposals.py::MemoryProposal`. */
export interface MemoryProposal {
  memory_key: string;
  agent: string;
  value_preview: string;
  created_at: string;
  graded_passes: number;
  graded_fails: number;
  used_count: number;
  harm_count: number;
  /** What the namespace's policy would decide now; "" when it would not yet. */
  ready: "" | "accept" | "reject";
}

export async function listProposals(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ proposals: MemoryProposal[]; total: number }> {
  return post("/v1/memory.proposals", { org: id.org, namespace: id.namespace, limit: 100 }, opts.signal);
}

export async function decideProposals(
  id: Scope,
  keys: string[],
  decision: "accept" | "reject",
): Promise<{ decided: number }> {
  return post("/v1/memory.proposal_decide", { org: id.org, namespace: id.namespace, memory_keys: keys, decision });
}

// ===== Revision suggestions for a conflict =====

export interface RevisionSuggestion {
  decision: "revise" | "contract" | "allow";
  target_local_id: number;
  peer_local_ids: number[];
  rationale: string;
}

/** POST /v1/conflicts.propose_revision: what to do about a conflict, changing nothing.
 *  `memories` maps each suggestion's local ids to keys. */
export async function proposeRevision(
  id: Scope,
  memoryId: string,
  peerId?: string,
): Promise<{
  found: boolean;
  memories: { local_id: number; memory_id: string; memory_key: string }[];
  suggestions: RevisionSuggestion[];
}> {
  return post("/v1/conflicts.propose_revision", {
    org: id.org,
    namespace: id.namespace,
    memory_id: memoryId,
    ...(peerId ? { peer_id: peerId } : {}),
  });
}
