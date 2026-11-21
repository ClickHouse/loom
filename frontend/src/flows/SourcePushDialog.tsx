import { useId, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { CopyButton } from "../components/CopyButton";
import { Alert, Loading, MetaList, MetaRow } from "../components/ui";
import { BusyButton } from "../components/ui/BusyButton";
import { configureSourcePush, confirmSourcePush, sourcePushStatus, type PushStatus } from "../api/sourcePush";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { DialogFrame } from "./TaskDialogFrame";

export function SourcePushDialog({
  org,
  namespace,
  name,
  kind,
  onClose,
}: {
  org: string;
  namespace: string;
  name: string;
  kind: string;
  onClose: () => void;
}) {
  const source = { org, namespace, name };
  const hintId = useId();
  const [status, setStatus] = useState<PushStatus | null>(null);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const refresh = useRef<HTMLButtonElement>(null);
  const revision = useRef(0);
  useAbortableEffect(
    (signal, live) => {
      const version = ++revision.current;
      sourcePushStatus(source, false, signal)
        .then((result) => live() && revision.current === version && setStatus(result))
        .catch((e) => live() && revision.current === version && setError(e));
    },
    [org, namespace, name],
  );

  const act = async (work: () => Promise<PushStatus>) => {
    if (busy) return;
    const version = ++revision.current;
    setBusy(true);
    setError(null);
    try {
      const result = await work();
      if (revision.current !== version) return;
      setStatus(result);
      setSecret("");
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      refresh.current?.focus();
    }
  };
  const pending = status?.status === "pending";
  const active = status?.status === "active";
  const configurable = status?.enabled && !active && !pending;
  return (
    <DialogFrame
      open
      onOpenChange={(open) => !open && onClose()}
      title={`Push notifications for ${name}`}
      description="Ask for a sync when the source changes. Regular polling continues if a notification is missed."
      footer={
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      }
    >
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!status && !error && <Loading label="Loading notification setup" />}
      {status && (
        <>
          {!status.enabled && (
            <Alert state="warning" text="An operator needs to enable push notifications on this deployment." />
          )}
          <MetaList>
            <MetaRow label="Status">
              {!status.enabled
                ? "Paused"
                : pending && kind !== "notion"
                  ? "Setup incomplete"
                  : (STATUS[status.status] ?? status.status)}
            </MetaRow>
            {status.channels > 0 && <MetaRow label="Channels">{status.channels}</MetaRow>}
            {status.expires_at > 0 && (
              <MetaRow label="Expires">{new Date(status.expires_at * 1000).toLocaleString()}</MetaRow>
            )}
          </MetaList>
          {status.callback_url && (
            <div className="space-y-2">
              <p className="text-sm">Notification address</p>
              <p className="font-mono text-xs break-all">{status.callback_url}</p>
              <CopyButton className="" text={status.callback_url} label="Copy notification address" />
            </div>
          )}
          {kind === "linear" && pending && status.enabled && (
            <div>
              <PasswordField
                label="Webhook signing secret"
                value={secret}
                onChange={setSecret}
                aria-describedby={hintId}
                autoComplete="off"
              />
              <p id={hintId} className="mt-1 text-xs text-text-muted">
                Create a webhook in Linear using the notification address above, then copy its signing secret here.
              </p>
              <BusyButton
                type="primary"
                htmlType="button"
                busy={busy}
                disabled={!secret.trim()}
                onClick={() => void act(() => confirmSourcePush(source, status, secret))}
              >
                Enable push
              </BusyButton>
            </div>
          )}
          {pending && kind === "notion" && status.enabled && (
            <div className="space-y-2">
              <p className="text-sm">
                Add the notification address to your Notion connection. Refresh after Notion sends its verification
                token.
              </p>
              {status.challenge_fingerprint && !status.verification_token && (
                <BusyButton
                  type="secondary"
                  htmlType="button"
                  busy={busy}
                  onClick={() => void act(() => sourcePushStatus(source, true))}
                >
                  Reveal verification token
                </BusyButton>
              )}
              {status.verification_token && (
                <>
                  <p className="font-mono text-xs break-all">{status.verification_token}</p>
                  <CopyButton className="" text={status.verification_token} label="Copy verification token" />
                  <p className="text-sm">
                    Paste this token into Notion and verify the subscription there, then confirm below.
                  </p>
                  <BusyButton
                    type="primary"
                    htmlType="button"
                    busy={busy}
                    onClick={() => void act(() => confirmSourcePush(source, status))}
                  >
                    I verified in Notion
                  </BusyButton>
                </>
              )}
            </div>
          )}
          {configurable && kind === "gdrive" && (
            <p className="text-sm">
              Drive setup watches the account and up to 19 shared drives it belongs to. An operator can enable automatic
              renewal.
            </p>
          )}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <BusyButton
          ref={refresh}
          type="secondary"
          htmlType="button"
          busy={busy}
          onClick={() => void act(() => sourcePushStatus(source))}
        >
          Refresh
        </BusyButton>
        {configurable && (
          <BusyButton
            type="primary"
            htmlType="button"
            busy={busy}
            onClick={() => void act(() => configureSourcePush(source, true))}
          >
            {kind === "gdrive" ? "Enable push" : "Start setup"}
          </BusyButton>
        )}
        {pending && status?.enabled && (
          <BusyButton
            type="secondary"
            htmlType="button"
            busy={busy}
            onClick={() => void act(() => configureSourcePush(source, true))}
          >
            Restart setup
          </BusyButton>
        )}
        {(active || pending || status?.status === "expired" || status?.status === "obsolete") && (
          <BusyButton
            type="secondary"
            htmlType="button"
            busy={busy}
            onClick={() => void act(() => configureSourcePush(source, false))}
          >
            Disable push
          </BusyButton>
        )}
      </div>
    </DialogFrame>
  );
}

const STATUS: Record<string, string> = {
  disabled: "Disabled",
  pending: "Waiting for verification",
  active: "Active",
  obsolete: "Source changed; set up again",
  expired: "Expired; set up again",
};
