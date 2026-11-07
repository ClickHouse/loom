// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { CodeId, RequestOpts } from "../api";
import type { TierHealth } from "../lib/tierHealth";

// ---------------------------------------------------------------------------
// Code plane (server/code/api/). Every input extends _CodeIdentity:
// org/namespace default to the deployment's code plane; `consumer` is trace
// attribution only. The UI always names the namespace explicitly because a
// namespace IS one repo@branch entry.
// ---------------------------------------------------------------------------

export interface CodeRepoSummary {
  org: string;
  namespace: string;
  repo: string;
  branch: string;
  provider?: string;
  profile?: "generic" | "clickhouse";
  compiler_candidates?: string[];
  quality_warnings?: string[];
  git_head: string;
  clone_head: string;
  fast_tier_lag: number;
  /** The ENTRY'S compiler tier (the one `scip_producer` names), not the C++ key by name —
   *  a TypeScript repository's scip_lag is its scip-typescript watermark against the tip.
   *  */
  scip_lag: number;
  scip_py_lag: number;
  scip_producer?: string;
  /** Each tier's last recorded outcome (fetch, ingest, the compiler tiers); a failing one
   *  names its error kind, since when, how far behind and the fix. */
  tier_health?: TierHealth[];
  indexed_at: string;
  last_refresh_at: string;
  counts: Record<string, number>;
  /** false only for admin-shaped callers without a grant: the entry is shown
   *  so it can be adopted, but nothing can be read until then. */
  readable?: boolean;
  /** How the namespace is joined and where the caller stands. `open` joins at once
   *  (read-only); `approval` pends on a maintainer; `closed` is not joinable. `my_status`
   *  is `member` with a live grant, `pending` with a request on file, else `none`. */
  join_policy?: "open" | "approval" | "closed";
  my_status?: "none" | "pending" | "member";
  /** The caller's active role on the entry ("admin" = maintainer), where the entry came
   *  from, and a self-service entry's first-index stage (queued → cloning → indexing →
   *  ready | error) with a JSON detail. */
  my_role?: "" | "viewer" | "writer" | "admin";
  source?: "config" | "registry";
  /** `has_maintainer`: an active admin grant
   *  exists on the branch namespace (fail-open true on a grants read failure, so adopt is
   *  never offered by mistake). `unreadable_reason`: why `readable` is false for this
   *  caller; `token_scope` = a restricted namespace where the caller holds a live grant
   *  reaching viewer but the sign-in does not name it. */
  /** The namespace's classification as resolved for this row ("restricted" | "standard");
   *  the card's pill reads it, never the viewer's own standing. */
  classification?: "standard" | "restricted";
  has_maintainer?: boolean;
  unreadable_reason?: "" | "token_scope" | "not_member" | "closed";
  /** `embedding` = the index is built, the vector leg still filling; `ready` is written only at LOOM_CODE_READY_EMBED_COVERAGE. */
  stage?: "" | "queued" | "cloning" | "indexing" | "embedding" | "ready" | "error";
  stage_detail?: string;
  /** Live chunks, those with a vector, and their ratio (0..1). */
  chunks_total?: number;
  chunks_embedded?: number;
  embedding_coverage?: number;
  /** The server's verdict: coverage has reached LOOM_CODE_READY_EMBED_COVERAGE, so the
   *  vector leg answers code.search. */
  searchable?: boolean;
  /** The namespace the repository's documents live in (its trunk branch's). */
  docs_namespace?: string;
  /** The trackers this branch links, primary first ([] = none), on a row the caller
   *  maintains; null/absent for a reader. */
  bound_trackers?: string[] | null;
}

export interface CodeRepoAddResult {
  org: string;
  namespace: string;
  repo: string;
  remote: string;
  provider: string;
  branch: string;
  join_policy: string;
  classification: string;
  index_stage: string;
  my_role: string;
  remote_head: string;
  /** Other branches registered in the same call, and per-branch refusals. */
  also?: { branch: string; namespace: string; remote_head: string }[];
  skipped?: { branch: string; reason: string }[];
}

/** Any signed-in person adds a repository index and becomes its first maintainer; the worker clones and builds it. */
export async function addCodeRepo(
  org: string,
  body: {
    repo: string;
    branch: string;
    join_policy: "open" | "approval";
    classification: "standard" | "restricted";
    access_token?: string;
    path_prefixes?: string[];
    exclude?: string[];
    /** Skip generated files (on by default) and the compiler tier ("" = inferred). */
    exclude_generated?: boolean;
    scip_producer?: "" | "go" | "typescript" | "clang" | "python";
    /** More branches chosen at add time, each its own index. */
    branches?: string[];
  },
): Promise<CodeRepoAddResult> {
  return post<CodeRepoAddResult>("/v1/code.repo.add", { org, ...body });
}

/** What a Git remote shows before anything is written — every branch, the
 *  default, the release-shaped ones, the ones indexed here already, and how many more
 *  indexes the org may add. */
export interface CodeRepoRemoteBranches {
  org: string;
  repo: string;
  remote: string;
  provider: string;
  warnings: string[];
  default_branch: string;
  /** The first `limit` matches of `query`, default and indexed always included. */
  branches: { name: string; sha: string }[];
  suggested: string[];
  indexed: { branch: string; namespace: string }[];
  cap: number;
  total: number;
  matched: number;
  truncated: boolean;
  query: string;
}
export async function remoteBranches(
  org: string,
  repo: string,
  access_token = "",
  query = "",
  limit = 60,
): Promise<CodeRepoRemoteBranches> {
  return post<CodeRepoRemoteBranches>("/v1/code.repo.remote_branches", { org, repo, access_token, query, limit });
}

/** A maintainer retires a repository index they added; `confirm` must equal the
 *  namespace. The rows leave through an erasure saga — `erasure_id` follows it under
 *  Audit → Erasures; the mirror goes when no other branch uses it. Operator-configured
 *  entries are refused. */
export interface CodeRepoRemoveResult {
  org: string;
  namespace: string;
  repo: string;
  branch: string;
  registry_deactivated: boolean;
  directory_deactivated: boolean;
  purged_tables: number;
  erasure_id: string;
  mirror_removed: boolean;
}
export async function removeCodeRepo(org: string, namespace: string): Promise<CodeRepoRemoveResult> {
  return post<CodeRepoRemoveResult>("/v1/code.repo.remove", { org, namespace, confirm: namespace });
}

/** A maintainer sets a repository's access — classification and join policy. Opening a
 *  restricted index needs `confirm` = namespace (the page passes it after the person
 *  types it); a restricted index is never `open`. */
export interface CodeRepoSetAccessResult {
  org: string;
  namespace: string;
  repo: string;
  classification: "standard" | "restricted";
  join_policy: "open" | "approval" | "closed";
  previous_classification: string;
  previous_join_policy: string;
  directory_updated: boolean;
  effective_within_s: number;
}
export async function setCodeRepoAccess(
  org: string,
  namespace: string,
  classification: "standard" | "restricted",
  join_policy: "open" | "approval" | "closed",
  confirm = "",
): Promise<CodeRepoSetAccessResult> {
  return post<CodeRepoSetAccessResult>("/v1/code.repo.set_access", {
    org,
    namespace,
    classification,
    join_policy,
    confirm,
  });
}

/** A maintainer binds the tracker record (issues, PRs, tests, incidents) a repository's
 *  tracker ops answer from; "" clears the binding to the deployment's. */
export interface CodeRepoSetSourceResult {
  org: string;
  namespace: string;
  /** The links as written (primary first) and as they were. */
  tracker_namespaces: string[];
  previous_tracker_namespaces: string[];
  effective_within_s: number;
  /** Linked trackers that do not list the repository (they answer nothing for it), and why. */
  not_covering: string[];
  warning: string;
}
/** The repository's trackers in order, primary first; [] unlinks all. */
export async function setCodeRepoSource(
  org: string,
  namespace: string,
  tracker_namespaces: string[],
): Promise<CodeRepoSetSourceResult> {
  return post<CodeRepoSetSourceResult>("/v1/code.repo.set_source", { org, namespace, tracker_namespaces });
}

/** A maintainer indexes another branch of a repository they maintain. */
export async function addCodeBranch(org: string, namespace: string, branch: string): Promise<CodeRepoAddResult> {
  return post<CodeRepoAddResult>("/v1/code.repo.add_branch", { org, namespace, branch });
}

export interface CodeWorkerStatus {
  enabled: boolean;
  interval_seconds: number;
  last_run_at: string;
  last_duration_ms: number;
  consecutive_failures: number;
  total_runs: number;
  last_error: string;
}

/** One compiler tier of a repository — the producer, the code_index_state key it
 *  advances, and where that stands against the tip. `primary` is the tier the entry is
 *  configured for. */
export interface CodeCompilerTier {
  producer: string;
  tool: string;
  label: string;
  watermark_key: string;
  commit: string;
  lag: number;
  updated_at: string;
  primary: boolean;
  workers: string[];
  note: string;
}

export interface CodeIndexStatus extends Omit<CodeRepoSummary, "indexed_at"> {
  scip_commit: string;
  scip_commit_py: string;
  /** Every compiler tier, the primary first; [] renders as "not configured" on the Health tab. */
  compiler_tiers: CodeCompilerTier[];
  pr_cursor: string;
  last_refresh_detail: string;
  /** Deployment-level read rule (LOOM_CODE_ORG_READABLE): when true, every
   *  org principal reads this entry regardless of namespace visibility. */
  org_readable?: boolean;
  path_prefixes: string[];
  state: Record<string, { value: string; detail: string; updated_at: string }>;
  workers: Record<string, CodeWorkerStatus>;
}

export async function codeRefresh(
  id: CodeId,
): Promise<{ triggered: boolean; already_running: boolean; head_before: string; head_after: string }> {
  return post("/v1/code.refresh", { ...id, agent: "loom-ui", wait: false });
}

export interface CodeUnit {
  unit_id: string;
  path: string;
  start_line: number;
  end_line: number;
  symbol: string;
  kind: string;
  signature: string;
  body: string;
  elided: boolean;
  description: string;
  tier: string;
  score: number;
  churn: number;
  amp: number;
  tested: boolean;
}

export interface CodeSearchResult {
  results: CodeUnit[];
  route: string;
  abstain: boolean;
  abstain_reason: string;
  retrieval_risk: "low" | "medium" | "high" | "abstain";
  retrieval_confidence: number;
  tokens_returned: number;
  trace_run_id?: string;
  abstain_code?: "" | "no_match" | "below_threshold" | "low_calibrated_confidence" | "not_covered";
  as_of?: CodeAsOf;
  /** "lexical only until embeddings finish (41% embedded)" below the ready threshold; absent/empty otherwise. */
  note?: string;
}

export async function codeSearch(
  id: CodeId,
  query: string,
  opts: { top_k?: number; path_prefix?: string } = {},
): Promise<CodeSearchResult> {
  return post<CodeSearchResult>("/v1/code.search", {
    ...id,
    query,
    top_k: opts.top_k ?? 10,
    path_prefix: opts.path_prefix ?? "",
  });
}

export interface CodeSymbolResult {
  definitions: CodeUnit[];
  ambiguous: boolean;
  candidates: Array<Record<string, unknown>>;
  tokens_returned: number;
}

export async function codeSymbol(id: CodeId, name: string): Promise<CodeSymbolResult> {
  return post<CodeSymbolResult>("/v1/code.symbol", {
    ...id,
    name,
    include_body: true,
    include_history: true,
    include_examples: false,
    include_lessons: false,
    token_budget: 4000,
  });
}

export interface CodeEdge {
  edge_kind: string;
  from_symbol: string;
  to_symbol: string;
  from_symbol_id: string;
  to_symbol_id: string;
  path: string;
  line?: number;
}

export interface CodeEdgesResult {
  edges: CodeEdge[];
  seed_symbols: string[];
  truncated: boolean;
}

export async function codeCallers(id: CodeId, name: string, depth = 1): Promise<CodeEdgesResult> {
  return post<CodeEdgesResult>("/v1/code.callers", {
    ...id,
    name,
    depth,
    max_nodes: 100,
  });
}

export async function codeCallees(id: CodeId, name: string): Promise<CodeEdgesResult> {
  return post<CodeEdgesResult>("/v1/code.callees", {
    ...id,
    name,
    depth: 1,
    max_nodes: 100,
  });
}

export async function codeMap(
  id: CodeId,
  opts: { token_budget?: number; module_prefix?: string } = {},
): Promise<{ map: string; tokens_used: number; commit_sha: string }> {
  return post("/v1/code.map", {
    ...id,
    token_budget: opts.token_budget ?? 1500,
    module_prefix: opts.module_prefix ?? "",
  });
}

export interface CodeLanguageCoverage {
  lang: string;
  files: number;
  files_errored: number;
  files_first_at: string;
  files_last_at: string;
  chunks: number;
  chunks_embedded: number;
  chunks_described: number;
  chunks_last_at: string;
  symbols_treesitter: number;
  symbols_treesitter_last_at: string;
  symbols_scip: number;
  symbols_scip_last_at: string;
}

export interface CodeCoverage {
  org: string;
  namespace: string;
  as_of_commit: string;
  languages: CodeLanguageCoverage[];
  tiers: Array<{ tier: string; edges: number; edges_last_at: string }>;
  gaps?: {
    functions_scanned: number;
    functions_uncalled: number;
    uncalled_share: number;
    source_files: number;
    source_files_untested: number;
    untested_share: number;
    code_files: number;
    code_files_no_symbols: number;
    no_symbols_share: number;
  } | null;
  pr_history_from: string;
  pr_history_to: string;
  pr_history_last_fetch_at: string;
  pr_history_enabled: boolean;
  pr_history_auth?: "token" | "anonymous" | "";
  context_packs_last_at: string;
  summaries_last_at: string;
}

export interface CodeIndexErrorRow {
  path: string;
  lang: string;
  error: string;
  indexed_commit: string;
  at: string;
}
export async function codeIndexErrors(
  id: CodeId,
  limit = 100,
  opts: RequestOpts = {},
): Promise<{ rows: CodeIndexErrorRow[]; total: number }> {
  return post("/v1/code.index_errors", { ...id, limit }, opts.signal);
}

export interface CodeRecentCommitRow {
  commit: string;
  subject: string;
  committed_at: string;
  ingested_at: string;
  files: number;
  pr_number: number;
}
export async function codeRecentCommits(
  id: CodeId,
  limit = 20,
  opts: RequestOpts = {},
): Promise<{ rows: CodeRecentCommitRow[] }> {
  return post("/v1/code.recent_commits", { ...id, limit }, opts.signal);
}

/** Provenance + freshness attached to every search/symbol/grep answer. */
export interface CodeAsOf {
  branch?: string;
  commit?: string;
  indexed_at?: string;
  /** first-parent commits the index trails the fetched tip by; -1 unknown */
  lag_commits?: number;
  /** lag_commits > LOOM_CODE_STALE_AFTER_COMMITS */
  stale?: boolean;
}

export async function codeCoverage(id: CodeId, opts: RequestOpts = {}): Promise<CodeCoverage> {
  return post<CodeCoverage>("/v1/code.coverage", id, opts.signal);
}

export interface CodeContextPackRow {
  pr_number: number;
  merge_commit_sha: string;
  pack_tokens: number;
  created_at: string;
  title: string;
  author: string;
  merged_at: string;
}

export async function listCodeContextPacks(
  id: CodeId,
  opts: { limit?: number; offset?: number; pr_number?: number } & RequestOpts = {},
): Promise<{ rows: CodeContextPackRow[]; total: number }> {
  return post(
    "/v1/code.context_packs",
    {
      ...id,
      limit: opts.limit ?? 50,
      offset: opts.offset ?? 0,
      pr_number: opts.pr_number ?? 0,
    },
    opts.signal,
  );
}

export async function codeContextPack(
  id: CodeId,
  pr_number: number,
  token_budget = 4000,
): Promise<{ context: string; tokens_used: number; source: "precomputed" | "orientation"; merge_commit_sha: string }> {
  return post("/v1/code.context_pack", {
    ...id,
    pr_number,
    token_budget,
  });
}
