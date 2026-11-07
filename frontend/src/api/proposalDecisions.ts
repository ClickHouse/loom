// memory.proposal_decide, apart from the listing: only the Review queue decides, and the
// listing also rides in the boot chunk for the sidebar's signal.
import { post } from "../api";
import type { Identity } from "../api";

export interface ProposalDecision {
  memory_key: string;
  decided: boolean;
  status?: string;
  reason?: string;
}

export async function decideProposal(
  id: Pick<Identity, "org" | "namespace">,
  memory_key: string,
  decision: "accept" | "reject",
  reason: string,
): Promise<{ decided: number; results: ProposalDecision[] }> {
  return post("/v1/memory.proposal_decide", {
    org: id.org,
    namespace: id.namespace,
    memory_keys: [memory_key],
    decision,
    reason,
  });
}
