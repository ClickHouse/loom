// The Review queue's duplicate, proposal and revision reads and decisions.
import { LONG_CALL_MS, post } from "../api";
import type { Identity, RequestOpts } from "../api";

export type Scope = Pick<Identity, "org" | "namespace">;

// ===== Duplicate memories (the dedup worker's pairs) =====

/** How many clusters each duplicate list asks for: the largest first. */
export const DUPLICATE_LIST_LIMIT = 50;

/** The fields of `server/memory/api/models_graph.py::_DedupCluster` the tab reads. */
export interface MemoryDuplicateCluster {
  canonical_id: string;
  canonical_key: string;
  canonical_agent: string;
  dup_count: number;
  duplicates: { memory_id: string; memory_key: string; agent: string }[];
}

export async function listMemoryDuplicates(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ clusters: MemoryDuplicateCluster[] }> {
  return post(
    "/v1/memory.dedup_list",
    { org: id.org, namespace: id.namespace, limit: DUPLICATE_LIST_LIMIT },
    opts.signal,
  );
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

/** The fields of `server/memory/api/models_graph.py::MemoryDedupCollapseOutput` the tab reads. */
export interface DuplicateCollapse {
  /** Search rows of duplicates still marked as live; a run marks them `duplicate`. */
  mirror_stale: number;
  restamped: number;
  truncated: boolean;
}

/** The most duplicate pairs one `memory.dedup_collapse` reads (the op's own maximum); it
 *  reads the same first pairs every call. */
export const COLLAPSE_PAIRS = 5000;

/** POST /v1/memory.dedup_collapse: marks the search rows of duplicates `duplicate`, so search
 *  skips them; no model call, and no value, key or memory changes. `dryRun` counts them. */
export async function collapseMemoryDuplicates(id: Scope, dryRun: boolean): Promise<DuplicateCollapse> {
  return post("/v1/memory.dedup_collapse", {
    org: id.org,
    namespace: id.namespace,
    dry_run: dryRun,
    limit: COLLAPSE_PAIRS,
  });
}

/** POST /v1/memory.resolve_orphan_duplicates: duplicates whose canonical is gone come back. */
export async function repairOrphanDuplicates(
  id: Scope,
  dryRun: boolean,
): Promise<{ candidates: number; reactivated: number }> {
  return post("/v1/memory.resolve_orphan_duplicates", { org: id.org, namespace: id.namespace, dry_run: dryRun });
}

/** How many of the agent's newest eligible memories a merge reads, and how old one must be. */
export const CONSOLIDATE_READ = 200;
// The Merge confirmation says "the last hour": change both together.
export const CONSOLIDATE_MIN_AGE_SECONDS = 3600;

/** POST /v1/memory.consolidate: one model call per cluster of near-duplicate memories of
 *  one agent (those of no user), merged into one each. */
export async function consolidateMemories(
  id: Scope,
  agent: string,
): Promise<{
  /** One entry per cluster merged. */
  consolidated: unknown[];
  examined: number;
  clusters_found: number;
  clusters_failed: number;
  clusters_refused_lossy: number;
}> {
  // One model call per cluster runs inside the call: the long bound, so a slow merge is
  // not reported as failed while it goes on.
  return post(
    "/v1/memory.consolidate",
    {
      org: id.org,
      namespace: id.namespace,
      agent,
      limit: CONSOLIDATE_READ,
      min_age_seconds: CONSOLIDATE_MIN_AGE_SECONDS,
    },
    AbortSignal.timeout(LONG_CALL_MS),
  );
}

/** POST /v1/memory.agents_list: the agents that wrote into the namespace. */
export async function listMemoryAgents(id: Scope, opts: RequestOpts = {}): Promise<string[]> {
  const r = await post<{ agents: string[] }>(
    "/v1/memory.agents_list",
    { org: id.org, namespace: id.namespace },
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
  return post(
    "/v1/knowledge.dedup_list",
    { org: id.org, namespace: id.namespace, limit: DUPLICATE_LIST_LIMIT },
    opts.signal,
  );
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

/** The fields of `server/memory/api/models_proposals.py::MemoryProposal` the tab reads. */
export interface MemoryProposal {
  memory_key: string;
  memory_id: string;
  agent: string;
  /** "" for a high row: its value is read by key. */
  value_preview: string;
  sensitivity: "" | "low" | "medium" | "high";
  created_at: string;
  graded_passes: number;
  graded_fails: number;
  /** What the namespace's policy would decide now; "" when it would not yet. */
  ready: "" | "accept" | "reject";
}

/** How many proposals the Review queue asks for, newest first. */
export const PROPOSAL_LIST_LIMIT = 100;

export async function listProposals(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ proposals: MemoryProposal[]; total: number }> {
  return post(
    "/v1/memory.proposals",
    { org: id.org, namespace: id.namespace, limit: PROPOSAL_LIST_LIMIT },
    opts.signal,
  );
}

export async function decideProposals(
  id: Scope,
  keys: string[],
  decision: "accept" | "reject",
): Promise<{ decided: number; results: { memory_key: string; decided: boolean; status: string; reason: string }[] }> {
  return post("/v1/memory.proposal_decide", { org: id.org, namespace: id.namespace, memory_keys: keys, decision });
}

// ===== Revision suggestions for a conflict =====

export interface RevisionSuggestion {
  decision: "revise" | "contract" | "allow";
  rationale: string;
}

/** POST /v1/conflicts.propose_revision: what to do about a conflict with all its peers,
 *  changing nothing. */
export async function proposeRevision(
  id: Scope,
  memoryId: string,
): Promise<{ found: boolean; suggestions: RevisionSuggestion[] }> {
  return post("/v1/conflicts.propose_revision", { org: id.org, namespace: id.namespace, memory_id: memoryId });
}
