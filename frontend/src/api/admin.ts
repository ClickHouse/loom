// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

// ===== Namespace usage audit =====

export type NamespaceAuditSeverity = "info" | "warning" | "critical";

/** Mirrors `server/identity/api/models.py::NamespaceAuditSummary`. */
export interface NamespaceAuditSummary {
  live_rows: number;
  live_bytes: number;
  // Bytes of content_excerpt the embedding rows carry.
  excerpt_bytes: number;
  version_rows: number;
  version_bytes: number;
  // Searchable-type embedding rows with no vector and a recorded embed error.
  unembedded_rows: number;
  // Ledger counts over `window_seconds`.
  gets: number;
  searches: number;
  sets: number;
  // gets / max(searches, 1): the object-store gauge.
  get_search_ratio: number;
  // Dashboard and assistant calls in the window, left out of the counts above.
  inspector_calls: number;
}

/** Mirrors `NamespaceAuditFinding`. `samples` carry keys and counts only —
 *  never a value; a sampled query is its redacted shape. */
export interface NamespaceAuditFinding {
  code: string;
  severity: NamespaceAuditSeverity;
  count: number;
  // 0..1 of the population the check looked at.
  share: number;
  bytes: number;
  samples: Array<Record<string, unknown>>;
  detail: string;
  remedy: string;
  // A broken stored invariant, or a pattern that may still be a legitimate design.
  // Absent from a server one release older, like every field below it.
  kind?: "contract_violation" | "usage_heuristic";
  rule_version?: number;
  source_op?: string;
  // Null for a rule that reads current state rather than the ledger window.
  window_seconds?: number | null;
  population?: number | null;
  sample_limit?: number;
  // False when the cap cut the samples or they cannot name every match (hot keys
  // against all gets).
  samples_complete?: boolean;
  // The live revision of each row the samples name: identifiers only, never a value.
  evidence?: Array<{
    memory_key: string;
    memory_id: string;
    version: number;
    content_sha256: string;
    agent: string;
    user_id: string;
    session_id: string;
    memory_type: string;
    source_class: string;
    updated_at: string;
  }>;
  exceptions?: string;
  uncertainty?: string;
}

/** Mirrors `NamespaceAuditOutput`. */
export interface NamespaceAuditResult {
  org: string;
  namespace: string;
  computed_at: string;
  window_seconds: number;
  summary: NamespaceAuditSummary;
  findings: NamespaceAuditFinding[];
  // Rules that could not run (checks, summary reads, the evidence read), as "name: reason".
  skipped: string[];
  // The rules that ran and were skipped; absent from a server one release older.
  coverage?: { rules_run: string[]; rules_skipped: string[]; complete: boolean };
  // No critical finding.
  healthy: boolean;
  criticals: number;
  warnings: number;
  infos: number;
}

/** POST /v1/namespace.audit — read-only; ~14 reads, budget 3 s, so it runs
 * on a click and never on mount. */
export async function auditNamespace(
  scope: { org: string; namespace: string },
  opts: { windowSeconds?: number; sampleLimit?: number } & RequestOpts = {},
): Promise<NamespaceAuditResult> {
  return post<NamespaceAuditResult>(
    "/v1/namespace.audit",
    {
      org: scope.org,
      namespace: scope.namespace,
      ...(opts.windowSeconds ? { window_seconds: opts.windowSeconds } : {}),
      ...(opts.sampleLimit != null ? { sample_limit: opts.sampleLimit } : {}),
    },
    opts.signal,
  );
}

// ===== Mentor =====

export type MentorSeverity = "critical" | "warning" | "info";

export interface MentorHint {
  code: string;
  severity: MentorSeverity;
  text: string;
  ops: string[];
  // The counts the rule fired on — checkable, never a value.
  evidence: Record<string, unknown>;
}

export interface MentorPlane {
  plane: string;
  calls: number;
  // The plane exists for this namespace; null when the mentor could not tell.
  available: boolean | null;
  when: string;
  // The mentor has a rule for this plane; others are listed, never "unused".
  mentored: boolean;
}

export interface MentorAdoption {
  code: string;
  shown_sessions: number;
  followed_sessions: number;
}

export interface MentorBriefResult {
  org: string;
  namespace: string;
  agent: string;
  window_seconds: number;
  computed_at: string;
  sessions: number;
  usage: Record<string, number>;
  planes: MentorPlane[];
  hints: MentorHint[];
  adoption: MentorAdoption[];
  // The namespace's writers in the window, most active first; `agent_defaulted`
  // says the brief picked the first because the caller had no agent name.
  agents: string[];
  agent_defaulted: boolean;
  skipped: string[];
  // No warning or critical hint.
  healthy: boolean;
}

/** The store's advice to one agent, with the evidence (`mentor.brief`). An
 *  empty `agent` means the caller's own. */
export async function mentorBrief(
  scope: { org: string; namespace: string },
  opts: { agent?: string; windowSeconds?: number } & RequestOpts = {},
): Promise<MentorBriefResult> {
  return post<MentorBriefResult>(
    "/v1/mentor.brief",
    {
      org: scope.org,
      namespace: scope.namespace,
      ...(opts.agent ? { agent: opts.agent } : {}),
      ...(opts.windowSeconds ? { window_seconds: opts.windowSeconds } : {}),
    },
    opts.signal,
  );
}

// ===== Consolidations =====

export type ConsolidationStatus = "written" | "skipped" | "failed";

export interface ConsolidationRunRow {
  run_id: string;
  memory_type: string;
  source_memory_ids: string[];
  /** The original memory_keys that were merged. */
  source_memory_keys: string[];
  cluster_size: number;
  cluster_cohesion: number;
  output_memory_key: string;
  output_memory_id: string;
  llm_provider: string;
  llm_model: string;
  tokens_in: number;
  tokens_out: number;
  duration_ms: number;
  status: ConsolidationStatus;
  failure_reason: string;
  ran_at: string;
}

// ===== Namespace flags =====

export interface NamespaceInfo {
  namespace: string;
  owner_email: string;
  description: string;
  tags: string[];
  active: boolean;
  conversation_logging_enabled?: boolean;
  /** null/undefined = inherit the server default; false = this namespace declined
   *  to have its prompt/completion text captured. */
  capture_generation_io?: boolean | null;
  daily_cost_cap_usd?: number;
  consolidation_enabled: boolean;
  conflict_threshold: number | null;
  visibility?: "private" | "public";
  requires_approval?: boolean;
  /** The stored join word and the resolved one. */
  join_policy?: "inherit" | "open" | "approval" | "closed";
  effective_join_policy?: "open" | "approval" | "closed";
  kind?: "memory" | "code" | "qa" | "bench";
  /** "restricted": only a named-namespace ADMIN token with a direct grant may update it. */
  classification?: "standard" | "restricted" | null;
  created_at: string;
  updated_at: string;
}

export async function getNamespace(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts = {},
): Promise<NamespaceInfo | null> {
  const r = await post<{ found: boolean } & NamespaceInfo>(
    "/v1/namespace.get",
    { org: id.org, namespace: id.namespace },
    opts.signal,
  );
  return r.found ? r : null;
}

/** Toggle whether this namespace records conversation transcripts.
 *  When disabled, conversation.append accepts calls but stores nothing. */
export async function setConversationLogging(
  id: Pick<Identity, "org" | "namespace">,
  enabled: boolean,
): Promise<{ namespace: string; enabled: boolean }> {
  return post("/v1/conversation.set_logging", {
    org: id.org,
    namespace: id.namespace,
    enabled,
  });
}

/** The fields namespace.register writes. */
export interface NamespaceRegisterFields {
  owner_email: string;
  description?: string;
  visibility?: "private" | "public";
  tags?: string[];
  active?: boolean;
  daily_cost_cap_usd?: number;
  consolidation_enabled?: boolean;
  conflict_threshold?: number | null;
  requires_approval?: boolean;
  /** Omit to keep the stored value. */
  join_policy?: "inherit" | "open" | "approval" | "closed";
  capture_generation_io?: boolean | null;
}

type RegisterResult = { registered: boolean; namespace: string; updated_at: string; visibility?: string };

/** Register or update the namespace. Doubles as the flag-flip op: a
 * re-register with a different flag value supersedes the prior row. */
export async function registerNamespace(
  id: Pick<Identity, "org" | "namespace">,
  opts: NamespaceRegisterFields,
): Promise<RegisterResult> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    owner_email: opts.owner_email,
    description: opts.description ?? "",
    visibility: opts.visibility ?? "private",
    tags: opts.tags ?? [],
    active: opts.active ?? true,
    daily_cost_cap_usd: opts.daily_cost_cap_usd ?? 0.0,
    consolidation_enabled: opts.consolidation_enabled ?? false,
    requires_approval: opts.requires_approval ?? false,
  };
  if (opts.conflict_threshold !== undefined) {
    body["conflict_threshold"] = opts.conflict_threshold;
  }
  // Sent only when explicitly set. Omitting it means "unchanged" server-side, so
  // an unrelated settings save cannot silently reset a privacy choice.
  if (opts.capture_generation_io !== undefined) {
    body["capture_generation_io"] = opts.capture_generation_io;
  }
  return post("/v1/namespace.register", body);
}

/** Rewrite an existing namespace's row with `patch` applied. The fields the settings
 * views show are carried from `ns` (a fresh namespace.get); every setting not sent here
 * keeps its stored value server-side. `tags` are replaced whole, so they are always sent. */
export async function reregisterNamespace(
  id: Pick<Identity, "org" | "namespace">,
  ns: NamespaceInfo,
  patch: Partial<NamespaceRegisterFields>,
): Promise<RegisterResult> {
  return registerNamespace(id, {
    owner_email: ns.owner_email,
    description: ns.description,
    visibility: ns.visibility ?? "private",
    tags: ns.tags,
    active: ns.active,
    daily_cost_cap_usd: ns.daily_cost_cap_usd ?? 0.0,
    consolidation_enabled: ns.consolidation_enabled,
    conflict_threshold: ns.conflict_threshold,
    requires_approval: ns.requires_approval ?? false,
    capture_generation_io: ns.capture_generation_io,
    ...patch,
  });
}

/** Soft-delete a namespace (sets active=false); existing memories +
 * grants stay queryable. Issue an `erasure.request` separately if the
 * data must go too. */
export async function deleteNamespace(
  id: Pick<Identity, "org" | "namespace">,
): Promise<{ deleted: boolean; namespace: string; updated_at: string }> {
  return post("/v1/namespace.delete", { org: id.org, namespace: id.namespace });
}

// ===== Consolidations =====

export async function listConsolidations(
  id: Pick<Identity, "org" | "namespace">,
  opts: { status?: ConsolidationStatus; limit?: number; before?: string } & RequestOpts = {},
): Promise<{ rows: ConsolidationRunRow[]; next_before: string | null }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    limit: opts.limit || 50,
  };
  if (opts.status) body["status"] = opts.status;
  if (opts.before) body["before"] = opts.before;
  return post("/v1/consolidations.list", body, opts.signal);
}

// ===== Admin: ops.health (ORG_ADMIN) =====

export interface OpsHealthCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  data: Record<string, unknown>;
  repair: string;
}

export interface OpsHealthResponse {
  checks: OpsHealthCheck[];
  overall: "ok" | "warn" | "fail";
}

/** The `op_mix` check's data block — create/read/update/delete counts for the window plus the two ratios that describe the shape. */
export interface OpMixData {
  window_days: number;
  counts: Record<string, number>;
  writes: number;
  reads_per_write: number | null;
  update_share: number | null;
  agents: number;
  top: { operation: string; count: number; class: string }[];
}

export async function getOpsHealth(
  id: Pick<Identity, "org" | "namespace">,
  opts: { namespace?: string | null } & RequestOpts = {},
): Promise<OpsHealthResponse> {
  const body: Record<string, unknown> = { org: id.org };
  if (opts.namespace) body["namespace"] = opts.namespace;
  return post("/v1/ops.health", body, opts.signal);
}

// ===== Admin: users =====

export interface UserNamespaceGrant {
  namespace: string;
  role: string;
  is_owner: boolean;
  granted_at: string | null;
  /** Target role the subject requested but isn't approved for yet.
   *  When non-null, an admin needs to Approve or Deny. */
  pending_role?: string | null;
}

export interface AdminUserRow {
  subject: string;
  contact_email: string;
  grants: UserNamespaceGrant[];
  is_platform_admin: boolean;
  last_active: string | null;
  has_password_auth: boolean;
}

export interface AdminUsersResult {
  users: AdminUserRow[];
  total: number;
  total_pending_elevations: number;
  total_active_last_7d: number;
  platform_admins_configured: string[];
}

/** Org-wide user list (platform admin only; the op refuses non-admin
 *  callers server-side). Aggregates subjects from namespace_grants +
 *  namespaces.owner_email + last-active on agent_events. */
export async function adminListUsers(
  id: Pick<Identity, "org"> & {
    namespace?: string;
    include_service_accounts?: boolean;
    limit?: number;
  },
  opts: RequestOpts = {},
): Promise<AdminUsersResult> {
  return post(
    "/v1/admin.users",
    {
      org: id.org,
      namespace: id.namespace,
      include_service_accounts: id.include_service_accounts ?? false,
      limit: id.limit ?? 200,
    },
    opts.signal,
  );
}

// ===== Erasure history =====

export interface ErasureListRow {
  erasure_id: string;
  scope_type: string;
  scope_value: string;
  requester: string;
  status: "queued" | "in_progress" | "completed" | "failed";
  requested_at: string;
  completed_at: string | null;
  failure_reason: string;
  steps_completed_count: number;
  /** Rows tombstoned by this erasure that were later restored. Non-zero
   *  means "completed" status overstates what's actually erased. */
  reversals_count: number;
}

/** Audit roll-up of every erasure ever issued in this org. Scoped
 *  by caller: platform/org admin sees all; others see only
 *  namespace-scoped erasures on their accessible namespaces. */
export async function listErasures(
  id: Pick<Identity, "org">,
  opts: { limit?: number; status?: ErasureListRow["status"] } & RequestOpts = {},
): Promise<{ rows: ErasureListRow[] }> {
  const body: Record<string, unknown> = {
    org: id.org,
    limit: opts.limit ?? 50,
  };
  if (opts.status) body["status"] = opts.status;
  return post("/v1/erasure.list", body, opts.signal);
}

// ===== Extraction Quality =====

export interface ExtractionQualityRow {
  namespace: string;
  pending: number;
  promoted: number;
  rejected: number;
  approve_ratio: number;
}

/** Approve / reject ratio per namespace for the learnings_queue.
 *  Pass namespace='' to get the org-wide rollup. */
export async function getExtractionQuality(
  id: Pick<Identity, "org"> & { namespace?: string; source_kind?: string },
  opts: RequestOpts = {},
): Promise<{ rows: ExtractionQualityRow[] }> {
  const body: Record<string, unknown> = { org: id.org };
  if (id.namespace) body["namespace"] = id.namespace;
  body["source_kind"] = id.source_kind ?? "conversation_seeder";
  return post("/v1/learnings.quality", body, opts.signal);
}

// ===== Blacklist =====

export type PatternType = "substring" | "regex";
export type BlacklistScope = "namespace" | "org" | "global";

export interface BlacklistRow {
  pattern_id: string;
  org: string;
  namespace: string;
  pattern: string;
  pattern_type: PatternType;
  scope: BlacklistScope;
  reason: string;
  created_by: string;
  hit_count: number;
  /** Stamped on every enforcement that matches this pattern; null on
   *  patterns that have never matched. */
  last_hit_at: string | null;
  created_at: string;
}

export async function listBlacklist(
  id: Pick<Identity, "org" | "namespace">,
  opts: { scope?: BlacklistScope; limit?: number } & RequestOpts = {},
): Promise<{ rows: BlacklistRow[] }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    limit: opts.limit || 100,
  };
  if (opts.scope) body["scope"] = opts.scope;
  return post("/v1/blacklist.list", body, opts.signal);
}

export async function addBlacklist(
  id: Pick<Identity, "org" | "namespace">,
  pattern: string,
  opts: {
    pattern_type?: PatternType;
    scope?: BlacklistScope;
    reason?: string;
    created_by?: string;
  } = {},
): Promise<{ pattern_id: string; created_at: string }> {
  return post("/v1/blacklist.add", {
    org: id.org,
    namespace: id.namespace,
    pattern,
    pattern_type: opts.pattern_type ?? "substring",
    scope: opts.scope ?? "namespace",
    reason: opts.reason ?? "",
    created_by: opts.created_by ?? "",
  });
}

export async function removeBlacklist(
  id: Pick<Identity, "org">,
  pattern_id: string,
  opts: { removed_by?: string; reason?: string } = {},
): Promise<{ removed: boolean; removed_at: string | null }> {
  return post("/v1/blacklist.remove", {
    org: id.org,
    pattern_id,
    removed_by: opts.removed_by ?? "",
    reason: opts.reason ?? "",
  });
}

// ===== History policy =====
//
// Rules over an agent's own recent history ("forbid memory.delete unless
// the caller ran memory.get on the same key within 1h"). policy.set /
// policy.delete are ADMIN; list / check / decisions are VIEWER. Wire
// shapes mirror server/identity/api/models_policy.py.

export type PolicyMode = "off" | "shadow" | "enforce";
export type PolicyEffect = "forbid" | "require";
export type PolicyDecisionKind = "allow" | "deny";

/** Condition grammar (validated server-side; kept opaque here):
 *  leaves  {op: "formerly"|"count_within"|"not_since", within: "1h",
 *           operation: string|string[], key?: "agent"|"session"|"user",
 *           success?: bool, args_equal?: {field: literal|"$args.<field>"}}
 *          count_within adds cmp: gt|gte|lt|lte|eq + value: int;
 *          not_since adds anchor: string|string[]
 *  nodes   {op: "all"|"any", of: [...]}, {op: "not", of: {...}} */
export type PolicyCondition = Record<string, unknown>;

export interface PolicyRule {
  policy_id: string;
  effect: PolicyEffect;
  actions: string[];
  condition: PolicyCondition;
  description: string;
  enabled: boolean;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export async function listPolicies(
  id: Pick<Identity, "org" | "namespace">,
  opts: { include_disabled?: boolean } & RequestOpts = {},
): Promise<{ rules: PolicyRule[]; mode: PolicyMode }> {
  return post(
    "/v1/policy.list",
    {
      org: id.org,
      namespace: id.namespace,
      include_disabled: opts.include_disabled ?? true,
    },
    opts.signal,
  );
}

export async function setPolicy(
  id: Pick<Identity, "org" | "namespace">,
  rule: {
    policy_id: string;
    effect: PolicyEffect;
    actions: string[];
    condition: PolicyCondition;
    description?: string;
    enabled?: boolean;
  },
): Promise<{ rule: PolicyRule; leaf_count: number }> {
  return post("/v1/policy.set", {
    org: id.org,
    namespace: id.namespace,
    policy_id: rule.policy_id,
    effect: rule.effect,
    actions: rule.actions,
    condition: rule.condition,
    description: rule.description ?? "",
    enabled: rule.enabled ?? true,
  });
}

export async function deletePolicy(
  id: Pick<Identity, "org" | "namespace">,
  policy_id: string,
): Promise<{ removed: boolean }> {
  return post("/v1/policy.delete", { org: id.org, namespace: id.namespace, policy_id });
}

export interface PolicyLeafResult {
  op: string;
  holds: boolean;
  value: unknown;
  error: string;
  /** Non-empty when the leaf was not evaluated (e.g. "args reference unresolved"). */
  skipped: string;
}

export interface PolicyRuleVerdict {
  policy_id: string;
  effect: string;
  /** null when a leaf errored and the rule could not be decided. */
  holds: boolean | null;
  denies: boolean;
  leaves: PolicyLeafResult[];
}

export interface PolicyCheckResponse {
  decision: PolicyDecisionKind;
  would_enforce: boolean;
  mode: string;
  write_shaped: boolean | null;
  determining_rules: string[];
  errors: string[];
  rules: PolicyRuleVerdict[];
  latency_ms: number;
}

/** Dry run: evaluate the stored rules (or `rules`, to test a candidate
 *  before saving) against a hypothetical call. Never blocks anything. */
export async function checkPolicy(
  id: Pick<Identity, "org" | "namespace">,
  req: {
    operation: string;
    agent?: string;
    user_id?: string;
    session_id?: string;
    args?: Record<string, unknown>;
    at?: string;
    rules?: PolicyCondition[];
  },
): Promise<PolicyCheckResponse> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    operation: req.operation,
    agent: req.agent || "-",
    user_id: req.user_id || "-",
    session_id: req.session_id || "-",
    args: req.args ?? {},
  };
  if (req.at) body["at"] = req.at;
  if (req.rules) body["rules"] = req.rules;
  return post("/v1/policy.check", body);
}

export interface PolicyDecisionRow {
  ts: string;
  agent: string;
  user_id: string;
  session_id: string;
  operation: string;
  request_id: string;
  mode: string;
  decision: string;
  enforced: boolean;
  determining_rules: string[];
  errors: string[];
  latency_ms: number;
}

export async function listPolicyDecisions(
  id: Pick<Identity, "org" | "namespace">,
  opts: { decision?: PolicyDecisionKind; limit?: number } & RequestOpts = {},
): Promise<{ rows: PolicyDecisionRow[] }> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    limit: opts.limit ?? 100,
  };
  if (opts.decision) body["decision"] = opts.decision;
  return post("/v1/policy.decisions", body, opts.signal);
}

// ===== cost rollup =====

// Shape backed by /v1/cost.summary — field names mirror the backend
// (loom.agent_cost_rollup MV). Do NOT rename here; the server controls
// these.
export type CostGroupDim = "namespace" | "agent" | "day" | "operation" | "memory_key" | "user_id";

export interface CostRow {
  day: string | null;
  agent: string | null;
  namespace: string | null;
  user_id: string | null;
  operation: string;
  ops: number;
  errors: number;
  tokens: number; // input + output combined per the MV
  embed_tokens: number;
  cost_usd: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
}

export async function listCostRollup(
  id: Pick<Identity, "org" | "namespace">,
  opts: { days?: number; groupBy?: CostGroupDim[]; orgWide?: boolean; includeCache?: boolean } & RequestOpts = {},
): Promise<{ rows: CostRow[] }> {
  // Backend cost.summary requires explicit from_date / to_date (YYYY-MM-DD).
  // Translate the simpler "last N days" UI knob into a date range here so
  // the view code stays unaware of the date plumbing.
  const days = opts.days ?? 7;
  const today = new Date();
  const from = new Date(today.getTime() - days * 24 * 60 * 60 * 1000);
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  const body: Record<string, unknown> = {
    org: id.org,
    from_date: fmt(from),
    to_date: fmt(today),
    // Always group by the dimensions we render: without group_by the
    // backend collapses to a single row with day=null / agent=null.
    group_by: opts.groupBy ?? ["day", "agent", "operation"],
    // The Usage page renders no cache savings, so it does not ask for them — the server's
    // cache statement scans the window's `args` map (about a fifth of the answer's
    // bytes).
    include_cache: opts.includeCache ?? false,
  };
  // Omit namespace for the org-wide operator view — the backend then scopes by
  // role (platform/org admin → every namespace; everyone else → their own).
  if (!opts.orgWide) body.namespace = id.namespace;
  return post("/v1/cost.summary", body, opts.signal);
}

// ===== memory.history / lineage / related (inspector wiring) =====

export interface MemoryHistoryEntry {
  // A namespace-wide history interleaves several writers' rows of the key.
  memory_id?: string;
  version: number;
  value: unknown;
  created_at: string;
  // When this version was written; `created_at` is the key's first write on every version.
  written_at?: string | null;
  tombstoned_at: string | null;
  redacted_at: string | null;
  source_trace: string;
  confidence?: number;
  content_sha256?: string;
  pinned?: boolean;
  // State a version can change with the value unchanged.
  status?: string;
  conflict_with?: string[];
  tags?: string[];
  verified_at?: string | null;
  verified_by?: string;
  verified_sha256?: string;
  lock_mode?: string;
  lock_reason?: string;
  lock_until?: string | null;
}

/** Version-by-version timeline for a single memory key. */
export async function memoryHistory(
  id: Identity,
  memoryKey: string,
  limit: number = 10,
): Promise<{ rows: MemoryHistoryEntry[] }> {
  return post("/v1/memory.history", {
    org: id.org,
    namespace: id.namespace,
    agent: id.agent,
    memory_key: memoryKey,
    limit,
  });
}

export interface RelatedMemoryRow {
  memory_key: string;
  shared_entities: string[];
  overlap_count: number;
}

/** Memories that share entities with this one. */
export async function memoryRelated(
  id: Identity,
  memoryKey: string,
  topK: number = 5,
): Promise<{ results: RelatedMemoryRow[] }> {
  return post("/v1/memory.related", {
    org: id.org,
    namespace: id.namespace,
    agent: id.agent,
    memory_key: memoryKey,
    top_k: topK,
  });
}

export interface LineageStep {
  step: string; // e.g. "extraction" | "promotion" | "consolidation"
  actor: string;
  ts: string;
  detail: string;
}

/** Trace back to the original write event + supporting learnings. */
export async function memoryLineage(id: Identity, memoryKey: string): Promise<{ steps: LineageStep[] }> {
  return post("/v1/memory.lineage", {
    org: id.org,
    namespace: id.namespace,
    agent: id.agent,
    memory_key: memoryKey,
  });
}

// ===== Deployment settings (platform admin) =====

export type SettingSource = "override" | "dashboard" | "env" | "env_file" | "settings_file" | "inferred" | "default";

/** Mirrors `server/insights/app/admin_models.py::SettingRow`. A secret's value is its length only. */
export interface SettingRow {
  name: string;
  env: string;
  value: unknown;
  default: unknown;
  source: SettingSource;
  // Takes a deployment-wide override while Loom runs; otherwise read once at startup.
  live: boolean;
  description: string;
}

/** Mirrors `SettingChange`: one recorded change, newest first in a history. */
export interface SettingChange {
  ts: string;
  actor: string;
  name: string;
  value: unknown;
  previous: unknown;
  // `override`: undo by setting `previous` back; another source was the changing
  // process's own value, undone with a reset. Null when the setting is one this build
  // lacks (the reset of a retired setting's override): nothing to undo.
  previous_source: string | null;
  cleared: boolean;
}

/** POST /v1/admin/settings.list — every setting's value in force on the replica that answers. */
export async function adminSettingsList(opts: RequestOpts = {}): Promise<SettingRow[]> {
  const out = await post<{ settings: SettingRow[] }>("/v1/admin/settings.list", {}, opts.signal);
  return out.settings;
}

/** POST /v1/admin/settings.set — override a live setting, refused (409) unless `expected`
 *  is still the value in force. */
export async function adminSettingSet(name: string, value: unknown, expected: unknown): Promise<SettingRow> {
  const out = await post<{ setting: SettingRow }>("/v1/admin/settings.set", { name, value, expected });
  return out.setting;
}

/** POST /v1/admin/settings.set with `clear` — back to the environment's value or the default. */
export async function adminSettingReset(name: string, expected: unknown): Promise<SettingRow> {
  const out = await post<{ setting: SettingRow }>("/v1/admin/settings.set", { name, clear: true, expected });
  return out.setting;
}

/** POST /v1/admin/settings.history — recorded changes, newest first. */
export async function adminSettingsHistory(name = "", limit = 50, opts: RequestOpts = {}): Promise<SettingChange[]> {
  const out = await post<{ changes: SettingChange[] }>("/v1/admin/settings.history", { name, limit }, opts.signal);
  return out.changes;
}
