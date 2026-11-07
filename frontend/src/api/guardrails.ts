// Guardrails and namespace settings: what extraction may store, whether the org's
// content may reach a model at all, a pattern's sweep, and memory webhooks.
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

/** Mirrors `server/memory/api/models_extraction_policy.py::ExtractionPolicyOutput`. */
export interface ExtractionPolicy {
  scope: PolicyScope;
  namespace: string;
  /** This scope's rules; "" when none is set. */
  policy: string;
  updated_by: string;
  updated_at: string | null;
  /** What extraction in this scope runs under: the org's rules, then the namespace's. */
  effective: string;
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

/** Mirrors `server/identity/api/models_model_calls.py::OrgModelCallsOutput`. `locked`: a platform admin's lock,
 *  under which the org's own admins can switch calls off but not on. */
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

// ===== Memory webhooks (namespace admin) =====

export const WEBHOOK_EVENTS = [
  "memory.written",
  "memory.revised",
  "memory.tombstoned",
  "memory.restored",
  "memory.redacted",
  "memory.erased",
] as const;
export type WebhookEvent = (typeof WEBHOOK_EVENTS)[number];

/** Mirrors `server/memory/api/models_webhook.py::WebhookInfo`. */
export interface Webhook {
  webhook_id: string;
  url: string;
  /** The environment variable holding the signing secret, never the secret. */
  secret_ref: string;
  events: WebhookEvent[];
  created_at: string;
  /** Whether the answering server holds the secret `secret_ref` names. */
  secret_present: boolean;
}

export async function listWebhooks(id: Scope, opts: RequestOpts = {}): Promise<{ webhooks: Webhook[] }> {
  return post("/v1/webhook.list", { org: id.org, namespace: id.namespace }, opts.signal);
}

export async function registerWebhook(
  id: Scope,
  url: string,
  secretRef: string,
  events: WebhookEvent[],
): Promise<{ webhook: Webhook }> {
  return post("/v1/webhook.register", { org: id.org, namespace: id.namespace, url, secret_ref: secretRef, events });
}

export async function removeWebhook(id: Scope, webhookId: string): Promise<{ removed: boolean }> {
  return post("/v1/webhook.remove", { org: id.org, namespace: id.namespace, webhook_id: webhookId });
}

/** Mirrors `server/memory/api/models_webhook.py::WebhookDelivery`. */
export interface WebhookDelivery {
  event_id: string;
  webhook_id: string;
  event_type: string;
  memory_key: string;
  occurred_at: string;
  status: "pending" | "delivered" | "dead";
  attempts: number;
  last_status: number;
  last_error: string;
}

export async function listWebhookDeliveries(
  id: Scope,
  webhookId: string,
  opts: RequestOpts = {},
): Promise<{ deliveries: WebhookDelivery[] }> {
  return post(
    "/v1/webhook.deliveries",
    { org: id.org, namespace: id.namespace, webhook_id: webhookId, limit: 50 },
    opts.signal,
  );
}
