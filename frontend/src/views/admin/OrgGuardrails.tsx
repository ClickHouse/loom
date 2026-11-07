import { useEffect, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Switch } from "@clickhouse/click-ui/Switch";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  clearExtractionPolicy,
  getExtractionPolicy,
  getOrgModelCalls,
  setExtractionPolicy,
  setOrgModelCalls,
  type ExtractionPolicy,
  type OrgModelCalls,
  type PolicyScope,
} from "../../api/guardrails";
import { useIdentity, useStore } from "../../lib/state";
import { hasPlatformAdminClaim } from "../../lib/role";
import { useAbortableEffect } from "../../lib/hooks";
import { humanizeError } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { ListState } from "../../components/ui/ListState";
import { Alert, ListTime, LoadingSkeleton, Panel, SectionHeading } from "../../components/ui";
import { SkeletonText } from "../../components/Skeleton";

// The server's cap on one scope's rules (server/memory/domain/extraction_policy.py).
const MAX_RULES_CHARS = 4096;
// The server's cap on a model-call switch's reason (server/identity/api/models_model_calls.py).
const MAX_REASON_CHARS = 500;

/**
 * The org admin's guardrails: the rules every extraction follows (the org's, and what this
 * namespace adds), and the switch that stops every model call for the org's content.
 * Both ops are ORG_ADMIN, so the page shows these panels to an org admin claim only.
 */
export function OrgGuardrails() {
  return (
    <div className="mt-8 space-y-6">
      <ModelCallsPanel />
      <ExtractionRules scope="org" />
      <ExtractionRules scope="namespace" />
    </div>
  );
}

function ExtractionRules({ scope }: { scope: PolicyScope }) {
  const identity = useIdentity();
  // Kept with the org and namespace it was read for, so an answer that lands after a
  // switch is not shown under the new one.
  const scopeKey = `${identity.org}/${scope === "namespace" ? identity.namespace : ""}`;
  const [loaded, setLoaded] = useState<{ scope: string; policy: ExtractionPolicy } | null>(null);
  const policy = loaded?.scope === scopeKey ? loaded.policy : null;
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (policy) setDraft(policy.policy);
  }, [policy]);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      getExtractionPolicy(identity, scope, { signal })
        .then((p) => live() && setLoaded({ scope: scopeKey, policy: p }))
        .catch((e) => live() && setError(e));
    },
    [scopeKey, reload],
  );

  const save = async (clear: boolean) => {
    const mine = scopeKey;
    setBusy(true);
    try {
      const p = clear
        ? await clearExtractionPolicy(identity, scope)
        : await setExtractionPolicy(identity, scope, draft.trim());
      // A save that lands after a switch leaves the new scope's rules alone.
      setLoaded((cur) => (cur && cur.scope !== mine ? cur : { scope: mine, policy: p }));
      toast(clear ? "Rules removed" : "Rules saved", "ok");
    } catch (e) {
      toast(`Save failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const title = scope === "org" ? "Extraction rules for the org" : `Extraction rules added for ${identity.namespace}`;
  const fieldId = `extraction-rules-${scope}`;
  const changed = policy !== null && draft.trim() !== policy.policy;
  return (
    <Panel>
      <SectionHeading
        label={title}
        level={2}
        caption={
          scope === "org"
            ? "What extraction never stores, in plain language. Every namespace follows them."
            : "Followed after the org's rules, in this namespace only."
        }
      />
      {!!error && (
        <ListState kind="error" things="extraction rules" error={error} onRetry={() => setReload((n) => n + 1)} />
      )}
      {!error && policy === null && (
        <LoadingSkeleton label="Loading extraction rules">
          <SkeletonText lines={2} />
        </LoadingSkeleton>
      )}
      {policy && (
        <div className="mt-3 space-y-3">
          <TextAreaField
            id={fieldId}
            label={scope === "org" ? "Rules for the org" : `Rules for ${identity.namespace}`}
            value={draft}
            onChange={setDraft}
            rows={4}
            maxLength={MAX_RULES_CHARS}
            placeholder="Never store credentials, payment card data or health data."
          />
          {policy.updated_by && (
            <p className="text-xs text-text-muted">
              Set by {policy.updated_by} {policy.updated_at && <ListTime time={policy.updated_at} quiet />}
            </p>
          )}
          <div className="flex justify-end gap-2">
            {policy.policy && (
              <Button
                type="secondary"
                htmlType="button"
                disabled={busy}
                onClick={async () => {
                  const ok = await confirmAction({
                    title: `Remove the ${scope}'s extraction rules?`,
                    confirmLabel: "Remove",
                    danger: true,
                  });
                  if (ok) {
                    void save(true);
                  }
                }}
              >
                Remove
              </Button>
            )}
            <Button
              type="primary"
              htmlType="button"
              disabled={busy || !changed || draft.trim() === ""}
              onClick={() => void save(false)}
            >
              Save rules
            </Button>
          </div>
        </div>
      )}
    </Panel>
  );
}

function ModelCallsPanel() {
  const identity = useIdentity();
  const platformAdmin = useStore((s) => hasPlatformAdminClaim(s.whoami));
  // Kept with the org it was read for, as the extraction rules are.
  const [loaded, setLoaded] = useState<{ org: string; calls: OrgModelCalls } | null>(null);
  const state = loaded?.org === identity.org ? loaded.calls : null;
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      getOrgModelCalls(identity.org, { signal })
        .then((calls) => live() && setLoaded({ org: identity.org, calls }))
        .catch((e) => live() && setError(e));
    },
    [identity.org, reload],
  );

  const flip = async (enabled: boolean) => {
    if (
      !enabled &&
      !(await confirmAction({
        title: `Stop every model call for ${identity.org}?`,
        message:
          "Until calls are switched back on, search and every write that embeds are refused, and no extraction or answer runs for the org's content. Reads by key and deletes still answer.",
        confirmLabel: "Switch off",
        danger: true,
      }))
    )
      return;
    setBusy(true);
    try {
      const org = identity.org;
      const calls = await setOrgModelCalls(org, enabled, reason.trim());
      setLoaded((cur) => (cur && cur.org !== org ? cur : { org, calls }));
      setReason("");
      toast(enabled ? "Model calls on" : "Model calls off", "ok");
    } catch (e) {
      toast(`Change failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  // Under a platform admin's lock the org's admins may still switch calls off, never on;
  // a platform admin may.
  const lockedOff = !!state?.locked && !state.enabled && !platformAdmin;
  return (
    <Panel>
      <SectionHeading
        label="Model calls"
        level={2}
        caption={`Whether Loom may send ${identity.org}'s content to a model at all.`}
      />
      {!!error && (
        <ListState kind="error" things="the model call setting" error={error} onRetry={() => setReload((n) => n + 1)} />
      )}
      {!error && state === null && (
        <LoadingSkeleton label="Loading the model call setting">
          <SkeletonText lines={2} />
        </LoadingSkeleton>
      )}
      {state && (
        <div className="mt-3 space-y-3">
          <TextField
            id="model-calls-reason"
            label="Reason (sent with the switch)"
            value={reason}
            onChange={setReason}
            maxLength={MAX_REASON_CHARS}
          />
          <div className="flex flex-wrap items-center gap-3">
            <Switch
              label="Model calls"
              checked={state.enabled}
              disabled={busy || lockedOff}
              onCheckedChange={(v) => void flip(v === true)}
            />
            <Badge text={state.enabled ? "On" : "Off"} state={state.enabled ? "success" : "danger"} size="sm" />
            {state.locked && <Badge text={`Locked by ${state.locked_by}`} state="warning" size="sm" />}
          </div>
          {lockedOff && (
            <Alert
              state="warning"
              text="A platform admin locked model calls off for this org; only they can switch them on."
            />
          )}
          {state.updated_by && (
            <p className="text-xs text-text-muted">
              Last changed by {state.updated_by} {state.updated_at && <ListTime time={state.updated_at} quiet />}
              {state.reason && <>: {state.reason}</>}
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
