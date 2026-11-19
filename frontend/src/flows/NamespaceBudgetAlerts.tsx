import { forwardRef, useImperativeHandle, useState, type KeyboardEvent } from "react";
import { NumberField } from "@clickhouse/click-ui/NumberField";
import { TextField } from "@clickhouse/click-ui/TextField";
import type { NamespaceInfo, NamespaceRegisterFields } from "../api";

interface Props {
  namespace: NamespaceInfo;
  locked: boolean;
  save: (patch: Partial<NamespaceRegisterFields>) => Promise<void>;
}

export interface BudgetAlertsHandle {
  cancelDrafts: () => boolean;
}

export const NamespaceBudgetAlerts = forwardRef<BudgetAlertsHandle, Props>(function NamespaceBudgetAlerts(
  { namespace, locked, save },
  ref,
) {
  const [userCap, setUserCap] = useState<string | null>(null);
  const [inbox, setInbox] = useState<string | null>(null);

  // Modal's capture handler runs before an input's bubbling Escape handler.
  useImperativeHandle(
    ref,
    () => ({
      cancelDrafts() {
        if (userCap === null && inbox === null) return false;
        setUserCap(null);
        setInbox(null);
        return true;
      },
    }),
    [userCap, inbox],
  );

  const commitUserCap = async () => {
    if (userCap === null) return;
    const value = Number(userCap);
    if (userCap.trim() && Number.isFinite(value) && value >= 0 && value !== (namespace.daily_user_cost_cap_usd ?? 0)) {
      await save({ daily_user_cost_cap_usd: value });
    }
    setUserCap(null);
  };

  const commitInbox = async () => {
    if (inbox === null) return;
    const value = inbox.trim();
    if (value.length <= 128 && value !== (namespace.budget_alert_inbox ?? "")) {
      await save({ budget_alert_inbox: value });
    }
    setInbox(null);
  };

  const keyDown = (event: KeyboardEvent<HTMLInputElement>, editing: boolean, reset: () => void) => {
    if (event.key === "Enter") event.currentTarget.blur();
    if (event.key === "Escape" && editing) {
      event.preventDefault();
      event.stopPropagation();
      reset();
    }
  };

  return (
    <>
      <div>
        <NumberField
          id="ns-user-cost-cap"
          label="Daily cap per user (USD)"
          aria-describedby="ns-user-cost-cap-help"
          min="0"
          step="0.01"
          value={userCap ?? String(namespace.daily_user_cost_cap_usd ?? 0)}
          disabled={locked}
          onChange={setUserCap}
          onBlur={() => void commitUserCap()}
          onKeyDown={(event) => keyDown(event, userCap !== null, () => setUserCap(null))}
        />
        <p id="ns-user-cost-cap-help" className="mt-1.5 text-xs leading-relaxed text-text-muted">
          Each named user has a separate cap within this namespace. Spend is counted over the last 24 hours. 0 inherits
          the server's per-user default. The namespace cap still applies.
        </p>
      </div>
      <div>
        <TextField
          id="ns-budget-inbox"
          label="Budget warning inbox"
          aria-describedby="ns-budget-inbox-help"
          value={inbox ?? namespace.budget_alert_inbox ?? ""}
          disabled={locked}
          maxLength={128}
          onChange={setInbox}
          onBlur={() => void commitInbox()}
          onKeyDown={(event) => keyDown(event, inbox !== null, () => setInbox(null))}
        />
        <p id="ns-budget-inbox-help" className="mt-1.5 text-xs leading-relaxed text-text-muted">
          Name an existing task inbox owned by this namespace. Leave blank to disable warnings. Budget checks send
          warnings at the server's configured share of the cap; user warnings require a check for that user.
        </p>
      </div>
    </>
  );
});
