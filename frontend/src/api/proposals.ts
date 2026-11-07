// Fact proposals (memory.proposals): facts an agent wrote with
// `memory.set(proposed=true)`, served only on request until someone accepts them.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

export interface MemoryProposal {
  memory_key: string;
  memory_id: string;
  version?: number;
  agent?: string;
  user_id?: string;
  session_id?: string;
  memory_type?: string;
  /** The value's first 400 characters; "" for a high row. */
  value_preview?: string;
  /** The value runs past the preview. */
  preview_cut?: boolean;
  sensitivity?: string;
  tags?: string[];
  created_at?: string;
  graded_passes?: number;
  graded_fails?: number;
  used_count?: number;
  harm_count?: number;
  /** What the namespace's policy thresholds say now. */
  ready?: "" | "accept" | "reject";
  conflict_with?: string[];
}

export interface ProposalList {
  proposals: MemoryProposal[];
  total: number;
}

export async function listProposals(
  id: Pick<Identity, "org" | "namespace">,
  limit: number,
  opts: RequestOpts = {},
): Promise<ProposalList> {
  return post<ProposalList>("/v1/memory.proposals", { org: id.org, namespace: id.namespace, limit }, opts.signal);
}
