// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { RequestOpts } from "../api";

// ---------------------------------------------------------------------------
// Entity graph (facts.traverse, facts.related, facts.path, facts.list) — the Graph explorer.
// ---------------------------------------------------------------------------

export interface TraversalNode {
  entity: string;
  hop: number;
  cumulative_weight: number;
  via_co_occur: number;
  via_relation: string;
  via_direction: string;
  community_id: number | null;
  // Typed walk only: "extracted" | "inferred" | "facet"; "" on co-occurrence.
  via_provenance?: string;
  // Validity window of the edge that reached this node; ISO text, "" when unbounded.
  valid_from?: string;
  valid_to?: string;
  // Chain from the seed to this node, seed first.
  path: string[];
  // Reported but not expanded by the hub policy.
  hub?: boolean;
  // The node's co-occurrence degree as the hub policy read it; 0 when off.
  degree?: number;
}

export interface TraverseResponse {
  seed: string;
  seed_community_id: number | null;
  nodes: TraversalNode[];
  // True when the server's node ceiling cut the walk short.
  truncated?: boolean;
  // Entities the hub policy held back from expanding.
  hubs?: string[];
}

export async function traverseFacts(
  id: { org: string; namespace: string },
  seed: string,
  opts: {
    max_hops?: number;
    top_k_per_hop?: number;
    labeled?: boolean;
    valid_as_of?: string;
    max_nodes?: number;
    relations?: string[];
  } & RequestOpts = {},
): Promise<TraverseResponse> {
  return post<TraverseResponse>(
    "/v1/facts.traverse",
    {
      org: id.org,
      namespace: id.namespace,
      seed_entity: seed,
      max_hops: opts.max_hops ?? 2,
      top_k_per_hop: opts.top_k_per_hop ?? 12,
      min_co_occur: 1,
      labeled: opts.labeled ?? false,
      max_nodes: opts.max_nodes ?? 200,
      ...(opts.relations && opts.relations.length ? { relations: opts.relations } : {}),
      ...(opts.valid_as_of ? { valid_as_of: opts.valid_as_of } : {}),
    },
    opts.signal,
  );
}

// One hop of co-occurrence neighbours (facts.related), for expanding a node
// in place: no path, no ring — the caller hangs each entity under the node it expanded.
export interface RelatedEntity {
  entity: string;
  co_occur: number;
  role: "subject" | "object" | "both";
}

export async function relatedFacts(
  id: { org: string; namespace: string },
  entity: string,
  opts: { top_k?: number; valid_as_of?: string } & RequestOpts = {},
): Promise<{ entity: string; related: RelatedEntity[] }> {
  return post(
    "/v1/facts.related",
    {
      org: id.org,
      namespace: id.namespace,
      entity,
      top_k: opts.top_k ?? 12,
      ...(opts.valid_as_of ? { valid_as_of: opts.valid_as_of } : {}),
    },
    opts.signal,
  );
}

// How two entities connect (facts.path): a bounded bidirectional search over
// the same validity-filtered edges the walk uses. `found=false` with
// `truncated=false` means not connected within `max_hops` on qualifying edges.
export interface PathEdge {
  entity_a: string;
  entity_b: string;
  relation: string;
  direction: string;
  co_occur: number;
  valid_from: string | null;
  valid_to: string | null;
}
export interface PathResponse {
  entity_a: string;
  entity_b: string;
  found: boolean;
  path: string[];
  edges: PathEdge[];
  hops: number;
  truncated: boolean;
}

export async function pathFacts(
  id: { org: string; namespace: string },
  a: string,
  b: string,
  opts: {
    max_hops?: number;
    top_k_per_hop?: number;
    labeled?: boolean;
    valid_as_of?: string;
    relations?: string[];
  } & RequestOpts = {},
): Promise<PathResponse> {
  return post<PathResponse>(
    "/v1/facts.path",
    {
      org: id.org,
      namespace: id.namespace,
      entity_a: a,
      entity_b: b,
      max_hops: opts.max_hops ?? 3,
      top_k_per_hop: opts.top_k_per_hop ?? 12,
      min_co_occur: 1,
      labeled: opts.labeled ?? false,
      ...(opts.relations && opts.relations.length ? { relations: opts.relations } : {}),
      ...(opts.valid_as_of ? { valid_as_of: opts.valid_as_of } : {}),
    },
    opts.signal,
  );
}

// Typed triples about an entity (facts.list), with the rows behind each and
// the "stale" flag — an open triple whose every source row is superseded.
export interface FactSourceRef {
  memory_key: string;
  status: string;
}
export interface FactRow {
  subject: string;
  relation: string;
  object: string;
  co_occur: number;
  last_seen: string;
  provenance: string;
  source_keys: string[];
  sources: FactSourceRef[];
  stale: boolean;
}

export async function listFacts(
  id: { org: string; namespace: string },
  // `entity` is subject OR object and wins over `subject`/`object`, which AND
  // together (the rows behind one edge). `provenance` keeps only those tiers.
  opts: {
    entity?: string;
    subject?: string;
    object?: string;
    limit?: number;
    valid_as_of?: string;
    provenance?: Array<"extracted" | "inferred" | "facet">;
  } & RequestOpts = {},
): Promise<{ rows: FactRow[]; truncated: boolean }> {
  return post(
    "/v1/facts.list",
    {
      org: id.org,
      namespace: id.namespace,
      ...(opts.entity ? { entity: opts.entity } : {}),
      ...(!opts.entity && opts.subject ? { subject: opts.subject } : {}),
      ...(!opts.entity && opts.object ? { object: opts.object } : {}),
      limit: opts.limit ?? 12,
      include_sources: true,
      include_source_status: true,
      ...(opts.provenance && opts.provenance.length ? { provenance: opts.provenance } : {}),
      ...(opts.valid_as_of ? { valid_as_of: opts.valid_as_of } : {}),
    },
    opts.signal,
  );
}
