import { useState } from "react";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Table } from "@clickhouse/click-ui/Table";
import { getExposureSummary, type ExposureRow } from "../../api";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { AgentBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { TableSkeleton } from "../../components/Skeleton";
import { OwnerReportSection } from "./OwnerReport";

const WINDOWS = [
  { value: "24", label: "24 hours" },
  { value: "168", label: "7 days" },
  { value: "720", label: "30 days" },
];

const HEADERS = [
  { label: "Token" },
  { label: "Agent" },
  { label: "Memories read", width: "130px" },
  { label: "Of the namespace", width: "130px" },
  { label: "Reads", width: "90px" },
  { label: "Last read", width: "120px" },
];

/** Which tokens read how much of the namespace (memory.exposure_summary): a token that has
 *  read most of it is the one to look at first when a token leaks. */
export function ExposurePanel() {
  const identity = useIdentity();
  const [hours, setHours] = useState(24);
  const [data, setData] = useState<{ namespace_rows: number; rows: ExposureRow[] } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setData(null);
      getExposureSummary(identity, hours, { signal })
        .then((d) => live() && setData(d))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, hours, reload],
  );

  return (
    <>
      <ListToolbar
        label="Time window"
        search={false}
        count={data ? `${data.rows.length} token${data.rows.length === 1 ? "" : "s"}` : undefined}
      >
        <ButtonGroup
          aria-label="Time window"
          options={WINDOWS}
          selected={String(hours)}
          onClick={(v) => setHours(Number(v))}
        />
      </ListToolbar>
      {!!error && <ListState kind="error" things="reads" error={error} onRetry={() => setReload((n) => n + 1)} />}
      {!error && data?.rows.length === 0 && (
        <ListState
          kind="empty"
          icon="eye"
          title="No reads in this window"
          description="Each search or read of this namespace's memories counts here, per token and agent."
        />
      )}
      {!error && (data === null || data.rows.length > 0) && (
        <Table
          aria-label="Memory reads by token"
          aria-busy={data === null || undefined}
          noDataMessage={data === null ? <TableSkeleton label="Loading reads" /> : undefined}
          size="sm"
          headers={HEADERS}
          rows={(data?.rows ?? []).map((r) => ({
            id: `${r.actor}/${r.agent}`,
            items: [
              { label: <span className="font-mono text-xs">{r.actor}</span> },
              { label: <AgentBadge agent={r.agent} /> },
              { label: <span className="tabular">{r.distinct_keys.toLocaleString()}</span> },
              { label: <span className="tabular">{Math.round(r.fraction * 100)}%</span> },
              { label: <span className="tabular">{r.calls.toLocaleString()}</span> },
              { label: <ListTime time={r.last_hour} quiet /> },
            ],
          }))}
        />
      )}
      <OwnerReportSection />
    </>
  );
}
