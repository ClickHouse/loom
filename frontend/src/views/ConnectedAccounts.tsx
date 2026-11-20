import { useEffect, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import {
  CONNECTION_KINDS,
  disconnectConnection,
  listConnections,
  startConnection,
  type ConnectionEntry,
  type ConnectionKind,
} from "../api/connections";
import { confirmAction } from "../components/ConfirmDialog";
import { toast, toastError } from "../components/Toast";
import { Loading, PageHeader } from "../components/ui";
import { BusyButton } from "../components/ui/BusyButton";
import { ListState } from "../components/ui/ListState";
import { SectionHeading } from "../components/ui/SectionHeading";
import { PageContainer } from "../layout/PageContainer";
import { canCallOwnConnections } from "../lib/canCall";
import { useAbortableEffect, useMounted } from "../lib/hooks";
import { goToConnectionConsent } from "../lib/navigation";
import { urlParam, useStore } from "../lib/state";

const LABELS: Record<ConnectionKind, string> = { gdrive: "Google Drive", notion: "Notion", linear: "Linear" };

export function ConnectedAccounts() {
  const whoami = useStore((s) => s.whoami);
  const org = useStore((s) => s.identity?.org ?? "");
  const authed = useStore((s) => s.authed);
  const expired = useStore((s) => s.sessionExpired);
  const setView = useStore((s) => s.setView);
  const available = authed && !expired && canCallOwnConnections(whoami) && whoami?.org === org;

  return (
    <PageContainer>
      <PageHeader
        title="Connected accounts"
        actions={
          <Button type="secondary" htmlType="button" onClick={() => setView("setup")}>
            Back to settings
          </Button>
        }
      />
      <p className="max-w-2xl text-sm text-text-muted mb-5">
        Connect your own account to use it with document sources. These connections belong to you in this organization.
      </p>
      {available && whoami?.subject ? (
        <OwnConnections key={JSON.stringify([org, whoami.subject])} org={org} subject={whoami.subject} />
      ) : (
        <p role="status" className="text-sm text-text-muted">
          Sign in with a personal account to manage connected accounts.
        </p>
      )}
    </PageContainer>
  );
}

function OwnConnections({ org, subject }: { org: string; subject: string }) {
  const mounted = useMounted();
  const actions = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    actions.current = controller;
    return () => controller.abort();
  }, []);
  const current = () => {
    const s = useStore.getState();
    return (
      mounted.current &&
      s.authed &&
      !s.sessionExpired &&
      s.identity?.org === org &&
      s.whoami?.org === org &&
      s.whoami.subject === subject &&
      canCallOwnConnections(s.whoami)
    );
  };
  const [connections, setConnections] = useState<ConnectionEntry[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reading, setReading] = useState(true);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<{ kind: ConnectionKind; disconnect: boolean } | null>(null);
  const busyRef = useRef(false);
  const navSeq = useStore((s) => s.navSeq);
  const asked = urlParam("kind");
  const selected = CONNECTION_KINDS.find((kind) => kind === asked);
  const providerList = useRef<HTMLUListElement>(null);
  const focusProvider = (kind: ConnectionKind) => {
    providerList.current?.querySelector<HTMLElement>(`#connection-${kind}`)?.focus();
  };
  useEffect(() => {
    if (selected && connections) focusProvider(selected);
  }, [selected, connections !== null, navSeq]);

  useAbortableEffect(
    async (signal, live) => {
      if (!current()) return;
      setReading(true);
      setError(null);
      try {
        const result = await listConnections(org, { signal });
        if (live() && current()) setConnections(result.connections);
      } catch (e) {
        if (live() && current()) setError(e);
      } finally {
        if (live() && current()) setReading(false);
      }
    },
    [org, subject, reload],
  );

  const act = async (entry: ConnectionEntry, disconnect: boolean) => {
    if (busyRef.current || reading || !current()) return;
    busyRef.current = true;
    setBusy({ kind: entry.kind, disconnect });
    try {
      if (disconnect) {
        const confirmed = await confirmAction({
          title: `Disconnect ${LABELS[entry.kind]}?`,
          message: "This stops your document sources that use this connection.",
          confirmLabel: "Disconnect",
          danger: true,
        });
        if (!confirmed || !current()) return;
        const result = await disconnectConnection(org, entry.kind, { signal: actions.current?.signal });
        if (!current()) return;
        if (result.disconnected) {
          focusProvider(entry.kind);
          setConnections(
            (rows) =>
              rows?.map((row) =>
                row.kind === entry.kind ? { ...row, connected: false, account_email: null, scopes: [] } : row,
              ) ?? null,
          );
          toast(`${LABELS[entry.kind]} disconnected`, "ok");
        }
        setReload((n) => n + 1);
      } else {
        const result = await startConnection(entry.kind, { signal: actions.current?.signal });
        if (current()) goToConnectionConsent(result.authorization_url);
      }
    } catch (e) {
      if (current()) toastError(disconnect ? "Disconnect failed" : "Couldn't connect account", e);
    } finally {
      busyRef.current = false;
      if (current()) setBusy(null);
    }
  };

  return (
    <section aria-label="Your connected accounts" aria-busy={connections !== null && reading}>
      <div className="mb-3 flex justify-end">
        <BusyButton
          type="secondary"
          htmlType="button"
          busy={reading}
          disabled={busy !== null}
          onClick={() => setReload((n) => n + 1)}
        >
          Refresh
        </BusyButton>
      </div>
      {!!error && (
        <ListState kind="error" things="connected accounts" error={error} onRetry={() => setReload((n) => n + 1)} />
      )}
      {!error && connections === null && <Loading label="Loading connected accounts" />}
      {!error && connections !== null && (
        <ul ref={providerList} className="divide-y divide-border">
          {CONNECTION_KINDS.map((kind) => {
            const entry = connections.find((row) => row.kind === kind);
            return (
              <li key={kind} className="flex flex-wrap items-center gap-3 py-4">
                <div className="min-w-0 flex-1 basis-40">
                  <SectionHeading id={`connection-${kind}`} label={LABELS[kind]} level={2} focusable />
                  {entry?.connected && entry.account_email && (
                    <p className="m-0 break-all text-sm text-text-muted">{entry.account_email}</p>
                  )}
                  <Badge
                    text={entry ? (entry.connected ? "Connected" : "Not connected") : "Unavailable"}
                    state={entry?.connected ? "success" : "neutral"}
                    size="sm"
                  />
                </div>
                {entry && (
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="secondary"
                      htmlType="button"
                      disabled={reading || busy !== null}
                      loading={busy?.kind === kind && !busy.disconnect}
                      aria-label={`${entry.connected ? "Reconnect" : "Connect"} ${LABELS[kind]}`}
                      onClick={() => void act(entry, false)}
                    >
                      {entry.connected ? "Reconnect" : "Connect"}
                    </Button>
                    {entry.connected && (
                      <Button
                        type="danger"
                        htmlType="button"
                        disabled={reading || busy !== null}
                        loading={busy?.kind === kind && busy.disconnect}
                        aria-label={`Disconnect ${LABELS[kind]}`}
                        onClick={() => void act(entry, true)}
                      >
                        Disconnect
                      </Button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
