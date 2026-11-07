// The Guardrails page's reads and writes: a pattern's sweep, what extraction may store,
// and whether the org's content may reach a model at all.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;

/** POST /v1/blacklist.sweep: tombstones the live rows a pattern already matches. */
export async function sweepBlacklistPattern(
  id: Scope,
  patternId: string,
): Promise<{ matched: number; tombstoned: number; truncated: boolean; error: string }> {
  return post("/v1/blacklist.sweep", { org: id.org, namespace: id.namespace, pattern_id: patternId });
}

// ===== Extraction policy (org admin) =====

export type PolicyScope = "org" | "namespace";

/** The fields of `server/memory/api/models_extraction_policy.py::ExtractionPolicyOutput` the page reads. */
export interface ExtractionPolicy {
  /** This scope's rules; "" when none is set. */
  policy: string;
  updated_by: string;
  updated_at: string | null;
}

function policyBody(id: Scope, scope: PolicyScope) {
  return { org: id.org, scope, namespace: scope === "namespace" ? id.namespace : "" };
}

export async function getExtractionPolicy(
  id: Scope,
  scope: PolicyScope,
  opts: RequestOpts = {},
): Promise<ExtractionPolicy> {
  return post("/v1/extraction_policy.get", policyBody(id, scope), opts.signal);
}

export async function setExtractionPolicy(id: Scope, scope: PolicyScope, policy: string): Promise<ExtractionPolicy> {
  return post("/v1/extraction_policy.set", { ...policyBody(id, scope), policy });
}

export async function clearExtractionPolicy(id: Scope, scope: PolicyScope): Promise<ExtractionPolicy> {
  return post("/v1/extraction_policy.clear", policyBody(id, scope));
}

// ===== The org's model calls (org admin) =====

/** The fields of `server/identity/api/models_model_calls.py::OrgModelCallsOutput` the page reads. `locked`:
 *  a platform admin's lock, under which the org's own admins can switch calls off but not on. */
export interface OrgModelCalls {
  enabled: boolean;
  reason: string;
  updated_by: string;
  updated_at: string | null;
  locked: boolean;
  locked_by: string;
}

export async function getOrgModelCalls(org: string, opts: RequestOpts = {}): Promise<OrgModelCalls> {
  return post("/v1/org.model_calls", { org }, opts.signal);
}

export async function setOrgModelCalls(org: string, enabled: boolean, reason: string): Promise<OrgModelCalls> {
  return post("/v1/org.set_model_calls", { org, enabled, reason });
}
