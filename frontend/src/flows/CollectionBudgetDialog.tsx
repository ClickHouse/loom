import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { getNamespace, setCollectionBudget, type CollectionBudgetPolicy, type CollectionEntry } from "../api";
import { collectionTitle } from "../lib/collections";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect, useMounted } from "../lib/hooks";
import { invalidateShared } from "../lib/sharedReads";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, Loading } from "../components/ui";
import { toast } from "../components/Toast";

/** Mounted per collection and open session, so a draft cannot target another collection. */
export function CollectionBudgetDialog({
  org,
  collection,
  onClose,
}: {
  org: string;
  collection: CollectionEntry;
  onClose: () => void;
}) {
  const mounted = useMounted();
  const [stored, setStored] = useState<CollectionBudgetPolicy | null>(null);
  const [cap, setCap] = useState("");
  const [userCap, setUserCap] = useState("");
  const [inbox, setInbox] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useAbortableEffect(
    async (signal, live) => {
      try {
        const row = await getNamespace({ org, namespace: collection.namespace }, { signal });
        if (!live()) return;
        if (!row) throw new Error("Collection settings not found");
        const policy = {
          daily_cost_cap_usd: row.daily_cost_cap_usd ?? 0,
          daily_user_cost_cap_usd: row.daily_user_cost_cap_usd ?? 0,
          budget_alert_inbox: row.budget_alert_inbox ?? "",
        };
        setStored(policy);
        setCap(String(policy.daily_cost_cap_usd));
        setUserCap(String(policy.daily_user_cost_cap_usd));
        setInbox(policy.budget_alert_inbox);
      } catch (e) {
        if (live()) setError(humanizeError(e));
      }
    },
    [org, collection.namespace],
  );

  const submit = async () => {
    if (!stored || busy) return;
    const patch: Partial<CollectionBudgetPolicy> = {};
    const next = {
      daily_cost_cap_usd: Number(cap),
      daily_user_cost_cap_usd: Number(userCap),
      budget_alert_inbox: inbox.trim(),
    };
    if (
      !cap.trim() ||
      !userCap.trim() ||
      !Number.isFinite(next.daily_cost_cap_usd) ||
      next.daily_cost_cap_usd < 0 ||
      !Number.isFinite(next.daily_user_cost_cap_usd) ||
      next.daily_user_cost_cap_usd < 0
    ) {
      setError("Caps must be finite, non-negative numbers");
      return;
    }
    if (next.daily_cost_cap_usd !== stored.daily_cost_cap_usd) patch.daily_cost_cap_usd = next.daily_cost_cap_usd;
    if (next.daily_user_cost_cap_usd !== stored.daily_user_cost_cap_usd)
      patch.daily_user_cost_cap_usd = next.daily_user_cost_cap_usd;
    if (next.budget_alert_inbox !== stored.budget_alert_inbox) patch.budget_alert_inbox = next.budget_alert_inbox;
    if (!Object.keys(patch).length) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await setCollectionBudget(org, collection.namespace, patch);
      invalidateShared(`budget:${org}/${collection.namespace}`);
      if (!mounted.current) return;
      toast("Budget settings saved", "ok");
      onClose();
    } catch (e) {
      if (mounted.current) setError(humanizeError(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <Modal
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title={`Budget · ${collectionTitle(collection)}`}
      className="w-full max-w-[480px]"
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          {!stored && !error && <Loading label="Loading collection budget" />}
          {stored && (
            <>
              <p id="collection-budget-help" className="text-sm text-text-muted">
                Caps count spend over the last 24 hours. 0 inherits the server default. The collection cap also applies
                to each named user.
              </p>
              <NumberField
                id="collection-cost-cap"
                label="Daily cost cap (USD)"
                aria-describedby="collection-budget-help"
                min="0"
                step="any"
                value={cap}
                onChange={setCap}
                disabled={busy}
              />
              <NumberField
                id="collection-user-cap"
                label="Daily cap per user (USD)"
                aria-describedby="collection-budget-help"
                min="0"
                step="any"
                value={userCap}
                onChange={setUserCap}
                disabled={busy}
              />
              <TextField
                id="collection-budget-inbox"
                label="Budget warning inbox"
                aria-describedby="collection-budget-inbox-help"
                maxLength={128}
                value={inbox}
                onChange={setInbox}
                disabled={busy}
              />
              <p id="collection-budget-inbox-help" className="text-sm text-text-muted">
                An existing task inbox owned by this collection. Leave blank to disable warnings.
              </p>
            </>
          )}
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!stored || busy} loading={busy}>
            Save budget
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
