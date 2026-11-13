// The `knowledge.gaps.*` client, imported from here rather than through the `api.ts`
// barrel: only the Library's gaps tile calls it.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

export interface KnowledgeGap {
  gap_id: string;
  /** The most asked form of the question, normalised. */
  head: string;
  /** The other questions merged into it. */
  samples: string[];
  askers: number;
  asks: number;
  last_at: string;
}

export interface KnowledgeGapList {
  gaps: KnowledgeGap[];
  built_at: string | null;
  /** Whether this deployment records misses at all. */
  enabled: boolean;
  /** False when capture is off for this namespace; null while `enabled` is false. */
  capturing: boolean | null;
}

/** POST /v1/knowledge.gaps.list (admin): the questions no memory or document answered. */
export async function listKnowledgeGaps(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts = {},
): Promise<KnowledgeGapList> {
  return post<KnowledgeGapList>(
    "/v1/knowledge.gaps.list",
    { org: id.org, namespace: id.namespace, limit: 10 },
    opts.signal,
  );
}
