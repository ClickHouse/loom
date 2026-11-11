import { useState } from "react";
import { useAbortableEffect } from "../../lib/hooks";
import { Badge } from "@clickhouse/click-ui/Badge";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Table } from "@clickhouse/click-ui/Table";
import { useStore } from "../../lib/state";
import { canCall } from "../../lib/canCall";
import { useNamespaceRole } from "../../lib/namespaceRole";
import { ForgetMemoriesDialog } from "../../flows/ForgetMemoriesDialog";
import { RowMenu } from "../../components/ui/RowMenu";
import { listConnectedAgents, type ConnectedAgentRow } from "../../api";
import { parseChTime } from "../../lib/format";
import { AgentBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { BusyButton } from "../../components/ui/BusyButton";
import { IconButton } from "../../components/IconButton";
import { PageContainer } from "../../layout/PageContainer";
import { ScopeBadge } from "../../components/ScopeBadge";
import { TableSkeleton } from "../../components/Skeleton";
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
  { label: "LLM cost" },
  { label: "Last seen" },
];
const ACTIONS_HEADER = { label: <span className="sr-only">Actions</span>, width: "52px" };

const WINDOWS = [
  { value: "1", label: "1 hour" },
  { value: "24", label: "24 hours" },
  { value: "168", label: "7 days" },
  { value: "720", label: "30 days" },
];

export function Agents() {
  const identity = useStore((s) => s.identity);
  const [windowHours, setWindowHours] = useState<number>(24);
  // The rows read, with the org and window they were read for: another's are not shown.
  const readFor = `${identity?.org}/${windowHours}`;
  const [read, setRead] = useState<{ key: string; rows: ConnectedAgentRow[] } | null>(null);
  const rows = read && read.key === readFor ? read.rows : null;
  const [error, setError] = useState<unknown>(null);
  // A refresh keeps the table on screen, marked busy, until it answers; a new window reads
  // as loading.
  const [refreshing, setRefreshing] = useState(false);
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity?.namespace);
  const canForget = canCall("memory.clear", role, whoami);
  const [forget, setForget] = useState<string | null>(null);
  const headers = canForget ? [...HEADERS, ACTIONS_HEADER] : HEADERS;

  // Refresh and Retry read again through the effect, so a newer read drops an older one.
  const [reads, setReads] = useState(0);
  const load = (signal: AbortSignal, live: () => boolean) => {
    if (!identity) return;
    setError(null);
    setRefreshing(true);
    const key = readFor;
    listConnectedAgents({ org: identity.org, window_hours: windowHours }, { signal })
      .then((r) => {
        if (live()) setRead({ key, rows: r.rows || [] });
      })
      .catch((e) => {
        if (live()) setError(e);
      })
      .finally(() => {
        if (live()) setRefreshing(false);
      });
  };
  useAbortableEffect(load, [identity?.org, windowHours, reads]);

  if (!identity) return null;

  return (
    <PageContainer>
      <PageHeader
        title="Agents"
        actions={
          <BusyButton
            type="secondary"
            htmlType="button"
            iconLeft="refresh"
            onClick={() => setReads((n) => n + 1)}
            busy={refreshing}
          >
            Refresh
          </BusyButton>
        }
      >
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

      <ListToolbar
        label="Time window and count"
        search={false}
        count={!error && rows !== null ? `${rows.length} client${rows.length === 1 ? "" : "s"}` : undefined}
      >
        <ButtonGroup
          aria-label="Time window"
          options={WINDOWS}
          selected={String(windowHours)}
          onClick={(value) => setWindowHours(Number(value))}
        />
      </ListToolbar>

      {!!error && (
        <ListState kind="error" things="connected clients" error={error} onRetry={() => setReads((n) => n + 1)} />
      )}
      {!error && rows === null && (
        <Table
          aria-label="Connected clients"
          aria-busy
          size="sm"
          headers={headers}
          rows={[]}
          noDataMessage={<TableSkeleton label="Loading connected clients" />}
        />
      )}
      {!error && rows !== null && rows.length === 0 && (
        <ListState
          kind="empty"
          icon="plug"
          title="No client has written in this window"
          description={
            windowHours <= 24
              ? "A client shows here once it writes. Connect an agent to set one up; a client you just set up writes after you restart it."
              : "Either no client has ever written to this org, or the window is too narrow. Try 7 days or 30 days."
          }
          connect="primary"
        />
      )}
      {!error && rows !== null && rows.length > 0 && (
        <div data-testid="agents-table" aria-busy={refreshing || undefined}>
          <Table
            aria-label="Connected clients"
            size="sm"
            headers={headers}
            rows={rows.map((r) => {
              const seen = parseChTime(r.last_seen);
              const stale = !seen || Date.now() - seen.getTime() > 24 * 3600 * 1000;
              return {
                id: r.agent,
                items: [
                  { label: <AgentBadge agent={r.agent} /> },
                  { label: <span className="tabular">{r.ops.toLocaleString()}</span> },
                  { label: <span className="tabular">{r.namespaces_touched}</span> },
                  { label: <span className="font-mono text-xs text-text-muted">{r.primary_namespace}</span> },
                  { label: <span className="tabular">{r.cost_usd > 0 ? `$${r.cost_usd.toFixed(4)}` : "—"}</span> },
                  {
                    label: (
                      // Not seen for a day: said by a badge, not by the colour of the time alone.
                      <span className="inline-flex items-center gap-1.5">
                        <ListTime time={r.last_seen} quiet className="text-text-muted" />
                        {stale && <Badge text="Stale" state="warning" size="sm" />}
                      </span>
                    ),
                  },
                  // `-` is no agent: memory.clear refuses it as a confirmation anyone can type.
                  ...(canForget
                    ? [
                        {
                          label: r.agent !== "-" && (
                            <RowMenu
                              name={r.agent}
                              items={[
                                {
                                  label: `Forget its memories in ${identity.namespace}`,
                                  icon: "trash",
                                  danger: true,
                                  onSelect: () => setForget(r.agent),
                                },
                              ]}
                            />
                          ),
                        },
                      ]
                    : []),
                ],
              };
            })}
          />
        </div>
      )}
      {forget && <ForgetMemoriesDialog agent={forget} onClose={() => setForget(null)} />}
    </PageContainer>
  );
}
