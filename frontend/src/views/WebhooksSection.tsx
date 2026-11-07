import { useState } from "react";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Table } from "@clickhouse/click-ui/Table";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  WEBHOOK_EVENTS,
  listWebhookDeliveries,
  listWebhooks,
  registerWebhook,
  removeWebhook,
  type Webhook,
  type WebhookDelivery,
  type WebhookEvent,
} from "../api";
import { useIdentity } from "../lib/state";
import { useAbortableEffect } from "../lib/hooks";
import { humanizeError } from "../lib/format";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { IconButton } from "../components/IconButton";
import { ListState } from "../components/ui/ListState";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, ListTime, Tip } from "../components/ui";

/**
 * The namespace's memory webhooks: a URL told of every memory write, revision, delete,
 * restore, redaction and erasure, signed with a secret the server reads from an
 * environment variable. Shown to the namespace's admin (the webhook ops are ADMIN).
 */
export function WebhooksSection() {
  const identity = useIdentity();
  const [hooks, setHooks] = useState<Webhook[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState(false);
  const [deliveries, setDeliveries] = useState<Webhook | null>(null);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      listWebhooks(identity, { signal })
        .then((r) => live() && setHooks(r.webhooks ?? []))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );
  const refresh = () => setReload((n) => n + 1);

  const remove = async (h: Webhook) => {
    if (!(await confirmAction({ title: `Stop sending events to ${h.url}?`, confirmLabel: "Remove", danger: true })))
      return;
    try {
      await removeWebhook(identity, h.webhook_id);
      refresh();
    } catch (e) {
      toast(`Remove failed: ${humanizeError(e)}`, "error");
    }
  };

  return (
    <section aria-labelledby="settings-webhooks">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-2xl">
          <h2 id="settings-webhooks" className="text-text">
            Webhooks
          </h2>
          <p className="text-xs text-text-muted mt-0.5">
            A URL Loom tells when a memory here is written, changed or deleted.
          </p>
        </div>
        <Button type="secondary" htmlType="button" iconLeft="plus" onClick={() => setAdding(true)}>
          Add webhook
        </Button>
      </div>
      <div className="mt-3">
        {!!error && <ListState kind="error" things="webhooks" error={error} onRetry={refresh} />}
        {!error && hooks?.length === 0 && <p className="text-sm text-text-muted">No webhook yet.</p>}
        {!error && !!hooks?.length && (
          <ul className="divide-y divide-border">
            {hooks.map((h) => (
              <li key={h.webhook_id} className="flex items-center gap-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="font-mono text-sm truncate">{h.url}</div>
                  <div className="text-xs text-text-muted">
                    {h.events.length === WEBHOOK_EVENTS.length ? "every event" : h.events.join(", ")} · secret{" "}
                    <span className="font-mono">{h.secret_ref}</span>
                  </div>
                </div>
                {!h.secret_present && <Badge text="Secret not set" state="warning" size="sm" />}
                <Button type="empty" htmlType="button" onClick={() => setDeliveries(h)}>
                  Deliveries
                </Button>
                <IconButton
                  icon="cross"
                  type="danger"
                  size="sm"
                  label={`Remove webhook ${h.url}`}
                  tooltip="Remove"
                  onClick={() => void remove(h)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
      {adding && (
        <AddWebhookDialog
          onClose={(added) => {
            setAdding(false);
            if (added) refresh();
          }}
        />
      )}
      {deliveries && <DeliveriesDialog hook={deliveries} onClose={() => setDeliveries(null)} />}
    </section>
  );
}

function AddWebhookDialog({ onClose }: { onClose: (added: boolean) => void }) {
  const identity = useIdentity();
  const [url, setUrl] = useState("");
  const [secretRef, setSecretRef] = useState("");
  const [events, setEvents] = useState<Set<WebhookEvent>>(() => new Set(WEBHOOK_EVENTS));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = url.trim() !== "" && secretRef.trim() !== "" && events.size > 0 && !busy;

  const toggle = (e: WebhookEvent, on: boolean) =>
    setEvents((cur) => {
      const next = new Set(cur);
      if (on) next.add(e);
      else next.delete(e);
      return next;
    });

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await registerWebhook(
        identity,
        url.trim(),
        secretRef.trim(),
        WEBHOOK_EVENTS.filter((e) => events.has(e)),
      );
      toast("Webhook added", "ok");
      onClose(true);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal open onOpenChange={(v) => !v && onClose(false)} title="Add webhook" className="w-full max-w-[520px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <TextField id="webhook-url" label="URL" value={url} onChange={setUrl} placeholder="https://" />
          <TextField
            id="webhook-secret-ref"
            label="Secret variable"
            value={secretRef}
            onChange={setSecretRef}
            placeholder="LOOM_WEBHOOK_SECRET_…"
          />
          <p className="text-xs text-text-muted">
            The name of the server's environment variable that holds the signing secret, never the secret itself.
          </p>
          <fieldset className="space-y-2">
            <legend className="text-sm text-text mb-1">Events</legend>
            {WEBHOOK_EVENTS.map((e) => (
              <Checkbox key={e} label={e} checked={events.has(e)} onCheckedChange={(c) => toggle(e, c === true)} />
            ))}
          </fieldset>
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => onClose(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!ready} loading={busy}>
            Add webhook
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}

const DELIVERY_STATE: Record<WebhookDelivery["status"], BadgeState> = {
  pending: "warning",
  delivered: "success",
  dead: "danger",
};

/** The webhook's events of the last three days and whether each arrived. */
function DeliveriesDialog({ hook, onClose }: { hook: Webhook; onClose: () => void }) {
  const identity = useIdentity();
  const [rows, setRows] = useState<WebhookDelivery[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  useAbortableEffect(
    (signal, live) => {
      listWebhookDeliveries(identity, hook.webhook_id, { signal })
        .then((r) => live() && setRows(r.deliveries ?? []))
        .catch((e) => live() && setError(e));
    },
    [hook.webhook_id],
  );
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title="Deliveries" className="w-full max-w-[760px]">
      <p className="text-sm text-text-muted mb-3 font-mono truncate">{hook.url}</p>
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!error && rows?.length === 0 && <p className="text-sm text-text-muted">No event in the last three days.</p>}
      {!error && rows !== null && rows.length > 0 && (
        <Table
          aria-label="Webhook deliveries"
          size="sm"
          headers={[{ label: "Event" }, { label: "Memory" }, { label: "Status" }, { label: "When", width: "110px" }]}
          rows={rows.map((d) => ({
            id: d.event_id,
            items: [
              { label: <span className="font-mono text-xs">{d.event_type}</span> },
              { label: <span className="font-mono text-xs">{d.memory_key}</span> },
              {
                label: (
                  <span className="inline-flex items-center gap-1.5">
                    {d.last_error ? (
                      <Tip content={d.last_error}>
                        <span>
                          <Badge text={d.status} state={DELIVERY_STATE[d.status]} size="sm" />
                        </span>
                      </Tip>
                    ) : (
                      <Badge text={d.status} state={DELIVERY_STATE[d.status]} size="sm" />
                    )}
                    {d.attempts > 0 && <span className="text-xs text-text-muted tabular">{d.attempts} tries</span>}
                  </span>
                ),
              },
              { label: <ListTime time={d.occurred_at} quiet /> },
            ],
          }))}
        />
      )}
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}
