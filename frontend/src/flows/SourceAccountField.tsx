import { useEffect, useRef, useState } from "react";
import { BusyButton } from "../components/ui/BusyButton";
import { listConnections, type ConnectionEntry, type ConnectionKind } from "../api/connections";
import { Alert } from "../components/ui/Alert";
import { ExternalLink } from "../components/ui/ExternalLink";
import { Loading } from "../components/ui/Loading";
import { canCallOwnConnections } from "../lib/canCall";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";

const LABELS: Record<ConnectionKind, string> = { gdrive: "Google Drive", notion: "Notion", linear: "Linear" };

/** A refresh invalidates a prior probe even when consent returns the same display email. */
export function useSourceAccount(org: string, kind: ConnectionKind | null, enabled: boolean) {
  const whoami = useStore((s) => s.whoami);
  const identityOrg = useStore((s) => s.identity?.org);
  const authed = useStore((s) => s.authed);
  const expired = useStore((s) => s.sessionExpired);
  const allowed = authed && !expired && identityOrg === org && canCallOwnConnections(whoami) && whoami?.org === org;
  const scope = JSON.stringify([org, whoami?.subject, kind, enabled, allowed]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const sequence = useRef(0);
  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<{
    scope: string;
    entry?: ConnectionEntry;
    error?: unknown;
    reading: boolean;
    version: number;
  }>({ scope: "", reading: true, version: 0 });
  const reload = () => setNonce((n) => n + 1);
  useEffect(() => {
    if (!enabled || !allowed) return;
    const refresh = () => setNonce((n) => n + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [enabled, allowed]);
  useAbortableEffect(
    async (signal, live) => {
      if (!enabled || !allowed || !kind) return;
      const version = ++sequence.current;
      setState({ scope, reading: true, version });
      try {
        const result = await listConnections(org, { signal });
        if (live() && currentScope.current === scope)
          setState({ scope, entry: result.connections.find((entry) => entry.kind === kind), reading: false, version });
      } catch (error) {
        if (live() && currentScope.current === scope) setState({ scope, error, reading: false, version });
      }
    },
    [scope, nonce],
  );
  const current = state.scope === scope;
  const entry = current ? state.entry : undefined;
  const reading = allowed && (!current || state.reading);
  return {
    allowed,
    entry,
    reading,
    reload,
    error: current ? state.error : undefined,
    ready: allowed && current && !reading && entry?.connected === true && entry.connect_available !== false,
    stamp: JSON.stringify([scope, state.version]),
  };
}

export function SourceAccountField({
  kind,
  account,
  busy,
}: {
  kind: ConnectionKind;
  account: ReturnType<typeof useSourceAccount>;
  busy: boolean;
}) {
  const label = LABELS[kind];
  if (!account.allowed) return <p role="status">Sign in with a personal account to choose your connected account.</p>;
  return (
    <section aria-label="Source account" aria-busy={account.reading} className="space-y-2 text-sm">
      {account.reading && <Loading label="Loading your connected account" layout="inline" />}
      {!!account.error && <Alert role="alert" state="danger" text={humanizeError(account.error, { read: true })} />}
      {!account.reading && !account.error && (
        <p role="status">
          {account.entry?.connect_available === false
            ? `An operator must configure ${label} connections on this deployment before you can use this account.`
            : account.entry?.connected
              ? `Using ${account.entry.account_email || `your ${label} account`}.`
              : `Connect your ${label} account to continue.`}
        </p>
      )}
      {account.entry?.connect_available !== false && (
        <div>
          <ExternalLink href={`/settings/connected-accounts?kind=${kind}&from=source`}>
            {account.entry?.connected ? `Manage ${label} connection` : `Connect ${label}`}
          </ExternalLink>
        </div>
      )}
      <p className="text-xs text-text-muted">After connecting, return to this tab. Your source details stay here.</p>
      <BusyButton type="secondary" htmlType="button" busy={account.reading} disabled={busy} onClick={account.reload}>
        Refresh account
      </BusyButton>
    </section>
  );
}
