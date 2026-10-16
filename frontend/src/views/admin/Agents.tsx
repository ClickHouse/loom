import { useState } from "react";
import { useAbortableEffect } from "../../lib/hooks";
import { Plug, RefreshCw } from "lucide-react";
import { useStore } from "../../lib/state";
import { listConnectedAgents, type ConnectedAgentRow } from "../../api";
import { relativeTime } from "../../lib/format";
import { Skeleton } from "../../components/Skeleton";
import { EmptyState } from "../../components/EmptyState";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";

/**
 * Agents — which AI clients have written to Loom in the last N hours.
 *
 * Rolls over agent_events, grouping every client request under its
 * self-identified `agent` (claude-desktop, cursor, claude-code, …): a
 * row appearing within seconds of a client write confirms the MCP
 * connection works. Troubleshooting copy lives in the rendered
 * description / empty states below.
 */
export function Agents() {
  const identity = useStore((s) => s.identity);
  const [windowHours, setWindowHours] = useState<number>(24);
  const [rows, setRows] = useState<ConnectedAgentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    if (!identity) return;
    setError(null);
    listConnectedAgents({ org: identity.org, window_hours: windowHours }, { signal })
      .then((r) => {
        if (live()) setRows(r.rows || []);
      })
      .catch((e) => {
        if (live()) setError(String(e?.message || e));
      });
  };
  useAbortableEffect(load, [identity?.org, windowHours]);

  if (!identity) return null;

  return (
    <PageContainer>
      <div className="flex items-baseline gap-3 mb-2">
        <h1 className="text-2xl font-semibold tracking-tight inline-flex items-center gap-2">
          <Plug size={20} /> Agents
        </h1>
      </div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <div className="text-sm text-[var(--color-text-muted)] mb-6 max-w-2xl">
        Every AI client that writes to Loom identifies itself in the request. This page rolls those requests up so you
        can see which clients are connected, when each was last active, and how much they're writing. If a client you
        expect isn't here within seconds of an action, check the config and restart the client.
      </div>

      <div className="flex items-center justify-between mb-4">
        <div className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden">
          {[1, 24, 168, 720].map((h) => (
            <button
              type="button"
              key={h}
              onClick={() => setWindowHours(h)}
              className={`px-3 py-1.5 text-xs font-medium border-r border-[var(--color-border)] last:border-r-0 ${
                windowHours === h
                  ? "bg-[var(--color-accent)] text-[var(--color-on-accent)]"
                  : "bg-[var(--color-surface)] hover:bg-[var(--color-surface-2)]"
              }`}
            >
              {h === 1 ? "1 hour" : h === 24 ? "24 hours" : h === 168 ? "7 days" : "30 days"}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => load()}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-[var(--color-border)] rounded-md hover:bg-[var(--color-surface-2)]"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      {error && <EmptyState icon={Plug} title="Couldn't load" description={error} />}
      {!error && rows === null && (
        <div className="space-y-2">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      )}
      {!error && rows !== null && rows.length === 0 && (
        <EmptyState
          icon={Plug}
          title="No client has written in this window"
          description={
            windowHours <= 24
              ? "If you just configured a client, make sure you restarted it. The Setup tab generates the MCP config; paste it into Claude Desktop / Cursor / VS Code and restart."
              : "Either no client has ever written to this org, or the window is too narrow. Try 7 days or 30 days."
          }
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-[var(--color-surface-2)]">
              <tr className="text-left text-xs text-[var(--color-text-muted)]">
                <th className="px-4 py-2.5">Client</th>
                <th className="px-4 py-2.5 text-right">Ops</th>
                <th className="px-4 py-2.5 text-right">Namespaces</th>
                <th className="px-4 py-2.5">Primary</th>
                <th className="px-4 py-2.5 text-right">LLM $</th>
                <th className="px-4 py-2.5 text-right">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const stale = Date.now() - new Date(r.last_seen).getTime() > 24 * 3600 * 1000;
                return (
                  <tr key={r.agent} className="border-t border-[var(--color-border)]">
                    <td className="px-4 py-2.5 font-mono">{r.agent}</td>
                    <td className="px-4 py-2.5 text-right tabular">{r.ops.toLocaleString()}</td>
                    <td className="px-4 py-2.5 text-right tabular">{r.namespaces_touched}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-[var(--color-text-muted)]">
                      {r.primary_namespace}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular">
                      {r.cost_usd > 0 ? `$${r.cost_usd.toFixed(4)}` : "—"}
                    </td>
                    <td
                      className={`px-4 py-2.5 text-right tabular ${stale ? "text-[var(--color-warn)]" : "text-[var(--color-text-muted)]"}`}
                    >
                      {relativeTime(r.last_seen)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </PageContainer>
  );
}
