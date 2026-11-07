import { useState } from "react";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Table } from "@clickhouse/click-ui/Table";
import { getExposureSummary, type ExposureRow, type ExposureSummary } from "../../api";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { AgentBadge, NameBadge } from "../../components/AgentBadge";
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
  { label: "Reader" },
  { label: "Agent" },
  { label: "Memories read", width: "130px" },
  { label: "Of the namespace", width: "130px" },
  { label: "Reads", width: "90px" },
  { label: "Last read", width: "120px" },
];

// The widest readers the summary is asked for; the toolbar says when there may be more.
const LIMIT = 200;

/** A share of the namespace, never rounded to 0% when something was read nor to 100%
 *  when something was not. Keys read
 *  include memories deleted since, so the fraction can pass 1, and a namespace emptied
 *  since answers 0 for any read: both read as all. */
function share({ fraction, distinct_keys }: ExposureRow, namespaceRows: number): string {
  if (fraction >= 1 || (namespaceRows === 0 && distinct_keys > 0)) return "all";
  if (fraction > 0 && fraction < 0.005) return "<1%";
  return `${Math.min(99, Math.round(fraction * 100))}%`;
}

/** Which readers (a principal through one agent) read how much of the namespace
 *  (memory.exposure_summary): one that has read most of it is the first to look at when a
 *  token leaks. */
export function ExposurePanel() {
  const identity = useIdentity();
  const [hours, setHours] = useState(24);
  const [data, setData] = useState<ExposureSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setData(null);
      getExposureSummary(identity, hours, LIMIT, { signal })
        .then((d) => live() && setData(d))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, hours, reload],
  );

  return (
    <>
      <ListToolbar label="Time window" search={false} count={data ? readerCount(data) : undefined}>
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
          description="Each search or read of this namespace's memories counts here, per reader and agent."
        />
      )}
      {!error && (data === null || data.rows.length > 0) && (
        <Table
          aria-label="Memory reads by reader"
          aria-busy={data === null || undefined}
          noDataMessage={data === null ? <TableSkeleton label="Loading reads" /> : undefined}
          size="sm"
          headers={HEADERS}
          rows={(data?.rows ?? []).map((r) => ({
            id: `${r.actor}/${r.agent}`,
            items: [
              { label: <NameBadge name={r.actor} /> },
              { label: <AgentBadge agent={r.agent} /> },
              { label: <span className="tabular">{r.distinct_keys.toLocaleString()}</span> },
              { label: <span className="tabular">{share(r, data?.namespace_rows ?? 0)}</span> },
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

function readerCount({ rows, namespace_rows }: ExposureSummary): string {
  const widest =
    rows.length >= LIMIT ? `the ${LIMIT} widest readers` : `${rows.length} reader${rows.length === 1 ? "" : "s"}`;
  return `${widest} of ${namespace_rows.toLocaleString()} memories`;
}
