// Settings → Memory's webhooks: a URL told of the namespace's memory changes, and its
// deliveries.
import { post } from "../api";
import type { Identity, RequestOpts } from "../api";

type Scope = Pick<Identity, "org" | "namespace">;

/** The start of every secret name `org` may use: `server/core/webhook_secrets.py`'s
 *  `webhook_secret_org_prefix`, a-z upper-cased, digits kept, every other byte `_` and hex. */
export function webhookSecretPrefix(org: string): string {
  let encoded = "";
  for (const b of new TextEncoder().encode(org)) {
    const c = String.fromCharCode(b);
    encoded += /[a-z0-9]/.test(c) ? c.toUpperCase() : `_${b.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return `LOOM_WEBHOOK_SECRET_${encoded}__`;
}

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

/** The fields of `server/memory/api/models_webhook.py::WebhookDelivery` the dialog reads. */
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

/** How many of a webhook's newest deliveries the dialog lists. */
export const DELIVERY_LIMIT = 50;

export async function listWebhookDeliveries(
  id: Scope,
  webhookId: string,
  opts: RequestOpts = {},
): Promise<{ deliveries: WebhookDelivery[] }> {
  return post(
    "/v1/webhook.deliveries",
    { org: id.org, namespace: id.namespace, webhook_id: webhookId, limit: DELIVERY_LIMIT },
    opts.signal,
  );
}
