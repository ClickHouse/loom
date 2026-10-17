import { useState } from "react";
import { useAbortableEffect } from "../../lib/hooks";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Table } from "@clickhouse/click-ui/Table";
import { useStore } from "../../lib/state";
import { listConnectedAgents, type ConnectedAgentRow } from "../../api";
import { relativeTime } from "../../lib/format";
import { EmptyState } from "../../components/EmptyState";
import { IconButton } from "../../components/IconButton";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { PageHeader } from "../../components/ui";

/**
 * Agents — which AI clients have written to Loom in the last N hours.
 *
 * Rolls over agent_events, grouping every client request under its
 * self-identified `agent` (claude-desktop, cursor, claude-code, …): a
 * row appearing within seconds of a client write confirms the MCP
 * connection works. Troubleshooting copy lives in the rendered
 * description / empty states below.
 */
const HEADERS = [
  { label: "Client" },
  { label: "Ops" },
  { label: "Namespaces" },
  { label: "Primary" },
  { label: "LLM $" },
  { label: "Last seen" },
];

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
  // A refresh or a new window keeps the table on screen, marked busy, until it answers.
  const [refreshing, setRefreshing] = useState(false);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    if (!identity) return;
    setError(null);
    setRefreshing(true);
    listConnectedAgents({ org: identity.org, window_hours: windowHours }, { signal })
      .then((r) => {
        if (live()) setRows(r.rows || []);
      })
      .catch((e) => {
        if (live()) setError(String(e?.message || e));
      })
      .finally(() => {
        if (live()) setRefreshing(false);
      });
  };
  useAbortableEffect(load, [identity?.org, windowHours]);

  if (!identity) return null;

  return (
    <PageContainer>
      <PageHeader title="Agents">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <div className="text-sm text-text-muted mb-4 max-w-[72ch]">
        The AI clients that write to Loom, when each was last active and how much it writes.{" "}
        <span className="inline-flex align-middle">
          <IconButton
            icon="info-in-circle"
            size="xs"
            label="About agents"
            tooltip="Every client names itself in its requests, and this page rolls them up. If a client you expect is not here within seconds of an action, check its config and restart it."
          />
        </span>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
        <ButtonGroup
          aria-label="Time window"
          options={WINDOWS}
          selected={String(windowHours)}
          onClick={(value) => setWindowHours(Number(value))}
        />
        <Button type="secondary" htmlType="button" iconLeft="refresh" onClick={() => load()} loading={refreshing}>
          Refresh
        </Button>
      </div>

      {error && <EmptyState icon="plug" title="Couldn't load" description={error} />}
      {!error && rows === null && (
        <Table aria-label="Connected clients" aria-busy loading size="sm" headers={HEADERS} rows={[]} />
      )}
      {!error && rows !== null && rows.length === 0 && (
        <EmptyState
          icon="plug"
          title="No client has written in this window"
          description={
            windowHours <= 24
              ? "If you just configured a client, make sure you restarted it. Settings generates the MCP config; paste it into Claude Desktop / Cursor / VS Code and restart."
              : "Either no client has ever written to this org, or the window is too narrow. Try 7 days or 30 days."
          }
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div data-testid="agents-table" aria-busy={refreshing || undefined}>
          <Table
            aria-label="Connected clients"
            size="sm"
            headers={HEADERS}
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
                      // Not seen for a day: said by a badge, not by the colour of the time alone.
                      <span className="inline-flex items-center gap-1.5">
                        <span className="tabular text-text-muted">{relativeTime(r.last_seen)}</span>
                        {stale && <Badge text="Stale" state="warning" size="sm" />}
                      </span>
                    ),
                  },
                ],
              };
            })}
          />
        </div>
      )}
    </PageContainer>
  );
}
