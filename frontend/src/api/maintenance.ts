// The Health page's namespace report and an operator's repairs.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;

// ===== Namespace health (viewer) =====

/** Mirrors `server/memory/api/models_admin.py::MemoryHealthOutput` (the fields read). */
export interface MemoryHealth {
  live: number;
  recalled_ever: number;
  silent_share: number;
  silent_after_days: number;
  staleness: { expiring_7d: number };
}

export async function getMemoryHealth(id: Scope, opts: RequestOpts = {}): Promise<MemoryHealth> {
  return post("/v1/memory.health", { org: id.org, namespace: id.namespace }, opts.signal);
}

/** Mirrors `server/memory/api/models_calibration.py::CalibrationGetOutput` (the gates). */
export interface Calibration {
  /** Which tier answered: the namespace's, the cluster's, or the scalar settings. */
  source: string;
  embedder_model: string;
  noise_floor: number;
  abstain_threshold: number;
  conflict_threshold: number;
  duplicate_threshold: number;
}

export async function getCalibration(id: Scope, opts: RequestOpts = {}): Promise<Calibration> {
  return post("/v1/calibration.get", { org: id.org, namespace: id.namespace }, opts.signal);
}

/** One deferred memory.set_from_messages batch. */
export interface ExtractionBatch {
  queue_id: string;
  agent: string;
  status: string;
  attempts: number;
  message_count: number;
  written: number;
  last_error: string;
  enqueued_at: string | null;
}

/** The newest batches in any state; the counts cover those batches only. */
export interface ExtractionBatches {
  batches: ExtractionBatch[];
  pending: number;
  failed: number;
}

/** POST /v1/memory.extraction_status, across the namespace's agents and users. */
export async function getExtractionBatches(id: Scope, opts: RequestOpts = {}): Promise<ExtractionBatches> {
  return post("/v1/memory.extraction_status", { org: id.org, namespace: id.namespace, agent: "*" }, opts.signal);
}

// ===== Repairs (namespace admin) =====

export interface ExpirySweep {
  scanned: number;
  tombstoned: number;
  demoted: number;
  anonymized: number;
  locked_skipped: number;
}

/** POST /v1/memory.sweep_expired: the expiry worker's pass, now, for this namespace. */
export async function sweepExpired(id: Scope, dryRun: boolean): Promise<ExpirySweep> {
  return post("/v1/memory.sweep_expired", { org: id.org, namespace: id.namespace, dry_run: dryRun });
}

/** POST /v1/memory.reindex: re-embed up to 200 memories another embedding model made. */
export async function reembedMemories(
  id: Scope,
): Promise<{ processed: number; skipped_already_current: number; target_model: string }> {
  return post("/v1/memory.reindex", { org: id.org, namespace: id.namespace, limit: 200 });
}

/** POST /v1/facts.expire_by_adapter: tombstone the graph facts one extractor emitted. */
export async function expireAdapterFacts(
  id: Scope,
  adapter: string,
): Promise<{ expired_triples: number; expired_triple_memories: number }> {
  return post("/v1/facts.expire_by_adapter", { org: id.org, namespace: id.namespace, adapter_name: adapter });
}

/** Mirrors `server/identity/api/models.py::NamespaceRollbackOutput` (the counts). */
export interface Rollback {
  reinstate: number;
  remove: number;
  truncated: boolean;
}

/** POST /v1/namespace.rollback: every memory head back to its version at `asOf` (ISO). */
export async function rollbackNamespace(id: Scope, asOf: string, dryRun: boolean): Promise<Rollback> {
  return post("/v1/namespace.rollback", { org: id.org, namespace: id.namespace, as_of: asOf, dry_run: dryRun });
}

/** POST /v1/namespace.snapshot: the namespace's curated state as one JSON payload. */
export async function snapshotNamespace(id: Scope): Promise<Record<string, unknown>> {
  return post("/v1/namespace.snapshot", { org: id.org, namespace: id.namespace });
}

/** POST /v1/namespace.restore: a snapshot payload into `target`. A snapshot already
 *  restored there answers `restored: false`. */
export async function restoreNamespace(
  org: string,
  target: string,
  payload: unknown,
): Promise<{ restored: boolean; counts: Record<string, number> }> {
  return post("/v1/namespace.restore", { org, target_namespace: target, payload });
}

// ===== Projection failures (org admin) =====

/** Mirrors `server/insights/api/models_ops.py::ProjectionFailureRow`. */
export interface ProjectionFailure {
  failure_id: string;
  namespace: string;
  projection: string;
  source_operation: string;
  memory_keys: string[];
  error_type: string;
  error_message: string;
  created_at: string | null;
}

export async function listProjectionFailures(
  id: Scope,
  opts: RequestOpts = {},
): Promise<{ rows: ProjectionFailure[]; total: number }> {
  return post("/v1/ops.projection_failures.list", { org: id.org, namespace: id.namespace }, opts.signal);
}

export async function retryProjectionFailures(
  id: Scope,
): Promise<{ retried: number; resolved: number; abandoned: number }> {
  return post("/v1/ops.projection_failures.retry", { org: id.org, namespace: id.namespace });
}

export async function resolveProjectionFailures(org: string, ids: string[]): Promise<{ resolved: number }> {
  return post("/v1/ops.projection_failures.resolve", { org, failure_ids: ids });
}
