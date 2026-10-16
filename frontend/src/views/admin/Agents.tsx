import { useState } from "react";
import { useAbortableEffect } from "../../lib/hooks";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Table } from "@clickhouse/click-ui/Table";
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
const WINDOWS = [
  { value: "1", label: "1 hour" },
  { value: "24", label: "24 hours" },
  { value: "168", label: "7 days" },
  { value: "720", label: "30 days" },
];

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
        <h1 className="text-2xl inline-flex items-center gap-2">
          <Icon name="plug" size="md" aria-hidden="true" /> Agents
        </h1>
      </div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <div className="text-sm text-text-muted mb-6 max-w-2xl">
        Every AI client that writes to Loom identifies itself in the request. This page rolls those requests up so you
        can see which clients are connected, when each was last active, and how much they're writing. If a client you
        expect isn't here within seconds of an action, check the config and restart the client.
      </div>

      <div className="flex items-center justify-between mb-4">
        <ButtonGroup
          aria-label="Time window"
          options={WINDOWS}
          selected={String(windowHours)}
          onClick={(value) => setWindowHours(Number(value))}
        />
        <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={() => load()}>
          Refresh
        </Button>
      </div>

      {error && <EmptyState icon="plug" title="Couldn't load" description={error} />}
      {!error && rows === null && (
        <div className="space-y-2">
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
          <Skeleton className="h-12" />
        </div>
      )}
      {!error && rows !== null && rows.length === 0 && (
        <EmptyState
          icon="plug"
          title="No client has written in this window"
          description={
            windowHours <= 24
              ? "If you just configured a client, make sure you restarted it. The Setup tab generates the MCP config; paste it into Claude Desktop / Cursor / VS Code and restart."
              : "Either no client has ever written to this org, or the window is too narrow. Try 7 days or 30 days."
          }
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <Table
          size="sm"
          headers={[
            { label: "Client" },
            { label: "Ops" },
            { label: "Namespaces" },
            { label: "Primary" },
            { label: "LLM $" },
            { label: "Last seen" },
          ]}
          rows={rows.map((r) => {
            const stale = Date.now() - new Date(r.last_seen).getTime() > 24 * 3600 * 1000;
            return {
              id: r.agent,
              items: [
                { label: <span className="font-mono">{r.agent}</span> },
                { label: <span className="tabular">{r.ops.toLocaleString()}</span> },
                { label: <span className="tabular">{r.namespaces_touched}</span> },
                { label: <span className="font-mono text-xs text-text-muted">{r.primary_namespace}</span> },
                { label: <span className="tabular">{r.cost_usd > 0 ? `$${r.cost_usd.toFixed(4)}` : "—"}</span> },
                {
                  label: (
                    <span className={`tabular ${stale ? "text-warn" : "text-text-muted"}`}>
                      {relativeTime(r.last_seen)}
                    </span>
                  ),
                },
              ],
            };
          })}
        />
      )}
    </PageContainer>
  );
}
