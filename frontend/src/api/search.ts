// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { Identity, RequestOpts, SearchScope } from "../api";

// ===== memory.search =====

export interface SearchHit {
  memory_key: string;
  memory_type: string;
  content_excerpt: string;
  distance: number;
  rank_score: number;
  confidence: number;
  namespace?: string;
  tags?: string[];
  /** Ingest time (ISO); "" when unknown. */
  created_at?: string;
  /** Writer identity; "" when the leg did not read it. */
  agent?: string;
  /** Server provenance note for the hit ("fact hit from namespace x;
   *  similarity=0.62; ..."). Rides at k ≤ 15 by default and at any k under
   * `explain: true`. */
  explanation?: string;
  /** Signed ranking terms (key_match, date_proximity_boost, entity, ...)
   *  beside the base `rank_score`; same gating as `explanation`. */
  score_components?: Record<string, number>;
}

/** Wrapper over POST /v1/memory.search for the Recall view.
 *  Use `memorySearchTrace` if you want per-result score breakdown. */
export async function searchMemory(
  id: Identity,
  query: string,
  opts: {
    search_mode?: "semantic" | "lexical" | "hybrid" | "graph";
    top_k?: number;
    memory_type?: "semantic" | "procedural" | "preference";
    pinned_only?: boolean;
    observation_date?: string;
    hyde?: boolean;
    entity_filter?: string[];
    tags?: string[];
    namespace_scope?: SearchScope;
    agent?: string;
    // How much of each hit's body to carry: "excerpt" is the first ~200
    // chars (the Graph panel's two-line clamp); the server default is "full".
    bodies?: "full" | "excerpt" | "none";
    // Pin the per-hit `explanation` / `score_components` on (or off) at any
    // k; unset follows the server's k ≤ 15 default.
    explain?: boolean;
    // The legs the caller can show. Unset = every leg. The Memories page passes
    // ["memory"]: it renders memory cards and never a session digest, and the
    // session-summary legs cost about a third of a keystroke's read.
    legs?: ("memory" | "sessions")[];
  } & RequestOpts = {},
): Promise<{
  results: SearchHit[];
  embedding_model: string;
  graph_seeds?: number;
  graph_neighbors_added?: number;
  triples_matched?: number;
  // Low-confidence signal: when set, the top hit is below the abstain
  // threshold — surface it so weak hits aren't shown as confident answers.
  abstain?: boolean;
  abstain_reason?: string;
  retrieval_risk?: string;
}> {
  const body: Record<string, unknown> = {
    // Operator browse: span every agent's memories by default (an explicit
    // agent filters to one). memory.search treats "*" as cross-agent; a
    // concrete agent would scope to that agent's own rows only.
    org: id.org,
    namespace: id.namespace,
    agent: opts.agent ?? "*",
    customer_id: "-",
    scope: "private",
    query,
    top_k: opts.top_k ?? 20,
    // Defer to the server's default ("hybrid": cosine + substring) when
    // the caller doesn't pick a mode.
    ...(opts.search_mode ? { search_mode: opts.search_mode } : {}),
    ...(opts.legs ? { legs: opts.legs } : {}),
  };
  if (opts.memory_type) body["memory_type"] = opts.memory_type;
  if (opts.pinned_only) body["pinned_only"] = true;
  if (opts.observation_date) body["observation_date"] = opts.observation_date;
  if (opts.hyde) body["hyde"] = true;
  if (opts.entity_filter && opts.entity_filter.length > 0) {
    body["entity_filter"] = opts.entity_filter;
  }
  if (opts.tags && opts.tags.length > 0) body["tags"] = opts.tags;
  if (opts.namespace_scope) body["namespace_scope"] = opts.namespace_scope;
  if (opts.bodies) body["bodies"] = opts.bodies;
  if (opts.explain !== undefined) body["explain"] = opts.explain;
  return post("/v1/memory.search", body, opts.signal);
}

/** A `memory.search_trace` candidate: the single VECTOR-leg relevance
 *  breakdown (cosine similarity + entity boost + blended rank_score), NOT the
 *  hybrid ranking `searchMemory` returns. Powers the Recall Lab. */
export interface SearchTraceHit {
  memory_key: string;
  memory_type: string;
  content_excerpt: string;
  distance: number;
  similarity: number;
  entity_boost: number;
  rank_score: number;
  rank: number;
  source: string;
}

/** POST /v1/memory.search_trace — per-candidate "why did this rank here"
 *  breakdown. This is the vector leg only; the production Recall list
 *  (`searchMemory`) runs the full hybrid pipeline, so the two orderings differ.
 *  The trace op ignores hybrid-only knobs (search_mode/pinned/hyde/tags), so we
 *  only expose what it actually consumes. */
export async function memorySearchTrace(
  id: Identity,
  query: string,
  opts: {
    top_k?: number;
    memory_type?: "semantic" | "procedural" | "preference";
    entity_filter?: string[];
    agent?: string;
  } = {},
): Promise<{ results: SearchTraceHit[]; embedding_model: string; entity_filter: string[] }> {
  const body: Record<string, unknown> = {
    // Operator browse: span every agent's memories by default ("*"), matching
    // searchMemory/listMemories. A concrete `id.agent` would scope to only the
    // session agent's rows and miss memories written by other agents.
    org: id.org,
    namespace: id.namespace,
    agent: opts.agent ?? "*",
    customer_id: "-",
    scope: "private",
    query,
    top_k: opts.top_k ?? 20,
  };
  if (opts.memory_type) body["memory_type"] = opts.memory_type;
  if (opts.entity_filter && opts.entity_filter.length > 0) {
    body["entity_filter"] = opts.entity_filter;
  }
  return post("/v1/memory.search_trace", body);
}

/** One past search, as recorded in `loom.search_trace_log`. */
export interface SearchTraceLogRow {
  ts: string;
  agent: string;
  /** The search wording. Empty when the namespace has not opted into text
   *  capture — the row is still written, so recall and latency stay visible
   *  without keeping what anyone typed. */
  query: string;
  returned_keys: string[];
  returned_scores: number[];
  top_k: number;
  result_count: number;
  abstain: boolean;
  retrieval_risk: string;
  embedding_model: string;
  latency_ms: number;
  run_id: string;
  /** The wire op that ran the search — memory.search, memory.ask,
   * context.pack or the MCP search tool. Empty on rows written before the column existed.
   *  */
  operation: string;
}

/** Searches that already happened, newest first (`memory.search_trace_log`).
 *
 *  Distinct from `memorySearchTrace`, which re-runs a query against memory as
 *  it is *now*. Only this can answer "my agent gave a bad answer earlier — what
 *  did it actually retrieve at the time", because the memory may have changed
 *  since. VIEWER role, and the server scopes rows to namespaces the caller can
 *  reach, so this is safe to show a non-operator their own history. */
export async function listSearchTraceLog(
  id: Identity,
  opts: { since_hours?: number; agent?: string; run_id?: string; limit?: number } = {},
): Promise<{ rows: SearchTraceLogRow[] }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    since_hours: opts.since_hours ?? 168,
    limit: opts.limit ?? 100,
  };
  // Omitted rather than sent as "*": the op filters on an exact agent, so a
  // wildcard would match nothing instead of everything.
  if (opts.agent) body["agent"] = opts.agent;
  if (opts.run_id) body["run_id"] = opts.run_id;
  return post("/v1/memory.search_trace_log", body);
}

export type McpClient = "claude-desktop" | "claude-code" | "cursor" | "vscode" | "cline" | "windsurf" | "zed";

export interface McpConfigResponse {
  client: McpClient;
  config_path: { darwin: string; linux: string; win32: string };
  config_key: string;
  config: Record<string, unknown>;
  token: string;
  subject: string;
  org: string;
  namespace: string;
  namespace_scope: "all" | "single";
  /** How reads are scoped. `inherited_grants`: the token reads every
   * namespace its creator holds a grant on, now and later. `named_namespaces`: only the
   * namespaces it names, while its creator still holds a grant there. */
  read_scope?: "inherited_grants" | "named_namespaces";
  /** Restricted namespaces this token reads by name. */
  restricted_namespaces?: string[];
  mcp_url: string;
  /** With persist=true, the token also lives in service_accounts
   *  (kind="mcp") and shows up in the unified Tokens list. */
  persisted?: boolean;
  sa_subject?: string;
}

export async function generateMcpConfig(
  client: McpClient,
  opts: { namespace?: string; ttl_days?: number | null; persist?: boolean; restricted_namespaces?: string[] } = {},
): Promise<McpConfigResponse> {
  const body: Record<string, unknown> = { client };
  if (opts.namespace) body["namespace"] = opts.namespace;
  // Restricted namespaces the token reads by name, each with a direct grant of its own
  // that is revoked with the token.
  if (opts.restricted_namespaces && opts.restricted_namespaces.length > 0)
    body["restricted_namespaces"] = opts.restricted_namespaces;
  if (opts.ttl_days !== undefined) body["ttl_days"] = opts.ttl_days;
  if (opts.persist) body["persist"] = true;
  return post<McpConfigResponse>("/v1/me/setup/mcp_config", body);
}
