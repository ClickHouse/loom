// The memory drawer's lifecycle actions and its Evidence and Links reads. Imported by the
// lazy drawer directly, not through `api.ts`, so the boot chunk does not carry them.
import { post } from "../api";
import type { Identity } from "../api";

export type LockMode = "read_only" | "no_delete";

/** POST /v1/memory.lock. `id` is the row's own identity (`rowIdentity`). */
export async function lockMemory(
  id: Identity,
  memoryKey: string,
  mode: LockMode,
  reason: string,
): Promise<{ locked: boolean; reason?: string }> {
  return post("/v1/memory.lock", { ...id, memory_key: memoryKey, mode, reason });
}

/** POST /v1/memory.unlock (ADMIN). */
export async function unlockMemory(id: Identity, memoryKey: string): Promise<{ unlocked: boolean; reason: string }> {
  return post("/v1/memory.unlock", { ...id, memory_key: memoryKey });
}

/** POST /v1/memory.reject: the fact is wrong; it is retired and not extracted again. */
export async function rejectMemory(id: Identity, memoryKey: string, reason: string): Promise<{ rejected: boolean }> {
  return post("/v1/memory.reject", { ...id, memory_key: memoryKey, rejection_reason: reason });
}

/** POST /v1/memory.promote (ADMIN): a copy in the namespace's shared layer, read by every agent. */
export async function shareMemory(id: Identity, memoryKey: string): Promise<{ shared_memory_key: string }> {
  return post("/v1/memory.promote", { ...id, memory_key: memoryKey });
}

/** POST /v1/memory.demote (ADMIN): the shared copy goes; the agent's own row stays. */
export async function unshareMemory(
  id: Pick<Identity, "org" | "namespace">,
  memoryKey: string,
): Promise<{ demoted: boolean }> {
  return post("/v1/memory.demote", { org: id.org, namespace: id.namespace, memory_key: memoryKey });
}

/** One source turn a memory was extracted from: `server/memory/api/models_lifecycle.py`. */
export interface MemoryEvidenceItem {
  evidence_id: string;
  scope: "cited" | "lexical" | "window" | "derived" | "other";
  role: string;
  observed_at: string | null;
  content: string;
  span: string;
}

export async function memoryEvidence(
  id: Identity,
  memoryKey: string,
): Promise<{ found: boolean; items: MemoryEvidenceItem[]; truncated: boolean }> {
  return post("/v1/memory.evidence", { ...id, memory_key: memoryKey, limit: 20 });
}

export interface MemoryBacklinkEdge {
  memory_key: string;
  relation: string;
  created_at: string;
}

/** POST /v1/memory.backlinks: the typed links into and out of a memory, and the session
 *  digests that cite it. */
export async function memoryBacklinks(
  id: Identity,
  memoryKey: string,
): Promise<{
  found: boolean;
  links_in: MemoryBacklinkEdge[];
  links_out: MemoryBacklinkEdge[];
  digests: { session_id: string; date: string }[];
}> {
  return post("/v1/memory.backlinks", { ...id, memory_key: memoryKey, limit: 20 });
}

/** POST /v1/memory.outcome: a person's verdict on one memory. `positive` adds evidence;
 *  `negative` records that relying on it went badly. */
export async function recordMemoryOutcome(
  id: Identity,
  memoryId: string,
  outcome: "positive" | "negative",
): Promise<{ recorded: boolean; quarantined: boolean }> {
  return post("/v1/memory.outcome", { ...id, memory_id: memoryId, outcome, source: "dashboard" });
}

/** POST /v1/memory.redact (ADMIN): scrub one past version's value, keeping its row and
 *  audit trail. The live version is refused (`refused_head`); `redacted: false` otherwise
 *  means the version is not recorded or the caller is not admin of its namespace. */
export async function redactMemoryVersion(
  org: string,
  memoryId: string,
  versionText: string,
  reason: string,
): Promise<{ redacted: boolean; refused_head: boolean }> {
  return post("/v1/memory.redact", { org, memory_id: memoryId, version: versionText, reason });
}
