// Memory reads and the copy action that only the lazy views call, kept out of `api.ts` so the
// boot chunk does not carry them. Importers still import from `api.ts`.
import { post } from "../api";
import type { CopyToResult, Identity, MemoryById, MemoryDetail, RequestOpts } from "../api";

/** `omit_high_value`: a high row answers without its value, and no sensitive read is
 *  audited, for a view that only names the row. */
export async function getMemoryById(
  org: string,
  memoryId: string,
  opts: { omit_high_value?: boolean } & RequestOpts = {},
): Promise<MemoryById> {
  const body: Record<string, unknown> = { org, memory_id: memoryId };
  if (opts.omit_high_value) body["omit_high_value"] = true;
  return post<MemoryById>("/v1/memory.get_by_id", body, opts.signal);
}

/** POST /v1/memory.bulk_get — N keys in one CH round-trip. `agent: "*"` is the
 *  namespace-wide lookup the Memories list needs: the page mixes writers, and under the
 *  wildcard the server opens the unset user/session axes the same way memory.get does.
 *  `results` holds the hits keyed by memory_key; `missing` the keys with no live row. */
export async function bulkGetMemories(
  id: Identity,
  keys: string[],
  opts: RequestOpts = {},
): Promise<{ results: Record<string, MemoryDetail>; missing: string[] }> {
  if (keys.length === 0) return { results: {}, missing: [] };
  return post("/v1/memory.bulk_get", { ...id, memory_keys: keys }, opts.signal);
}

// Self-service copy of a memory into another namespace (memory.copy_to).
// Non-destructive: the source stays put; the target row is re-embedded under
// the target namespace's own embedder. Needs WRITER on the target namespace.
export async function copyMemoryTo(
  source: Identity,
  memoryKey: string,
  toNamespace: string,
  opts: { targetKey?: string; rationale?: string } = {},
): Promise<CopyToResult> {
  return post<CopyToResult>("/v1/memory.copy_to", {
    from_org: source.org,
    from_namespace: source.namespace,
    memory_key: memoryKey,
    // A row opened under its user and session is copied as that exact row; otherwise the
    // newest row of the key, whoever wrote it, as the Memories list shows it.
    ...(source.user_id || source.session_id
      ? { from_agent: source.agent, from_user_id: source.user_id ?? "-", from_session_id: source.session_id ?? "-" }
      : { from_agent: "*" }),
    to_namespace: toNamespace,
    target_key: opts.targetKey ?? null,
    rationale: opts.rationale ?? "",
  });
}
