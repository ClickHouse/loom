// Fact proposals (memory.proposals): facts an agent wrote with
// `memory.set(proposed=true)`, served only on request until someone accepts them.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

export interface MemoryProposal {
  memory_key: string;
  memory_id: string;
  agent?: string;
  memory_type?: string;
  /** The first 400 characters of the value; "" for a high-sensitivity row. */
  value_preview?: string;
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
  limit = 50,
  opts: RequestOpts = {},
): Promise<ProposalList> {
  return post<ProposalList>("/v1/memory.proposals", { org: id.org, namespace: id.namespace, limit }, opts.signal);
}
