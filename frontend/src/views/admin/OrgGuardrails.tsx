import { useState } from "react";
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
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { humanizeError } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { ListState } from "../../components/ui/ListState";
import { Alert, ListTime, LoadingSkeleton, Panel, SectionHeading } from "../../components/ui";
import { SkeletonText } from "../../components/Skeleton";

// The server's cap on one scope's rules (server/memory/domain/extraction_policy.py).
const MAX_RULES_CHARS = 4096;

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
  const [policy, setPolicy] = useState<ExtractionPolicy | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setPolicy(null);
      getExtractionPolicy(identity, scope, { signal })
        .then((p) => {
          if (!live()) return;
          setPolicy(p);
          setDraft(p.policy);
        })
        .catch((e) => live() && setError(e));
    },
    [identity.org, scope === "namespace" ? identity.namespace : "", reload],
  );

  const save = async (clear: boolean) => {
    setBusy(true);
    try {
      const p = clear
        ? await clearExtractionPolicy(identity, scope)
        : await setExtractionPolicy(identity, scope, draft.trim());
      setPolicy(p);
      setDraft(p.policy);
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
            label="Rules"
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
                  if (await confirmAction({ title: `Remove the ${scope}'s extraction rules?`, danger: true })) {
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
  const [state, setState] = useState<OrgModelCalls | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setState(null);
      getOrgModelCalls(identity.org, { signal })
        .then((s) => live() && setState(s))
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
          "No embedding, search ranking, extraction or answer runs for the org's content until calls are switched back on. Reads by key and deletes still work.",
        confirmLabel: "Switch off",
        danger: true,
      }))
    )
      return;
    setBusy(true);
    try {
      setState(await setOrgModelCalls(identity.org, enabled, reason.trim()));
      setReason("");
      toast(enabled ? "Model calls on" : "Model calls off", "ok");
    } catch (e) {
      toast(`Change failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  // Under a platform admin's lock the org's admins may still switch calls off, never on.
  const lockedOff = !!state?.locked && !state.enabled;
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
      {state && (
        <div className="mt-3 space-y-3">
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
          <TextField id="model-calls-reason" label="Reason for the next change" value={reason} onChange={setReason} />
        </div>
      )}
    </Panel>
  );
}
