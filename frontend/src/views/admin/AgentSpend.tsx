import { useState } from "react";
import { Table } from "@clickhouse/click-ui/Table";
import { getAgentSpend, type AgentSpendSummary } from "../../api";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { ListState } from "../../components/ui/ListState";
import { TableSkeleton } from "../../components/Skeleton";
import { SectionHeading } from "../../components/ui";

const DAYS = 7;

/** The coding agents' own model spend in this namespace (cost.agent_summary), by model. */
export function AgentSpend() {
  const identity = useIdentity();
  const [data, setData] = useState<AgentSpendSummary | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setData(null);
      getAgentSpend(identity, DAYS, { signal })
        .then((d) => live() && setData(d))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );
  const unpriced = data?.unpriced_turns ? `, ${data.unpriced_turns} turns unpriced` : "";
  return (
    <section className="mt-8">
      <SectionHeading
        label="Coding agents"
        level={2}
        caption={
          data
            ? `$${data.total_cost_usd.toFixed(4)} over the last ${DAYS} days${unpriced}`
            : `What the coding agents spent on their own model calls, the last ${DAYS} days.`
        }
      />
      {!!error && (
        <ListState kind="error" things="coding agent spend" error={error} onRetry={() => setReload((n) => n + 1)} />
      )}
      {!error && data?.rows.length === 0 && <p className="text-sm text-text-muted">No coding agent reported spend.</p>}
      {!error && (data === null || data.rows.length > 0) && (
        <Table
          aria-label="Coding agent spend"
          aria-busy={data === null || undefined}
          noDataMessage={data === null ? <TableSkeleton label="Loading coding agent spend" /> : undefined}
          size="sm"
          headers={[{ label: "Model" }, { label: "Turns" }, { label: "Sessions" }, { label: "Cost" }]}
          rows={(data?.rows ?? []).map((r) => {
            const model = r.model || "—";
            return {
              id: model,
              items: [
                { label: <span className="font-mono text-xs">{model}</span> },
                { label: <span className="tabular">{r.turns.toLocaleString()}</span> },
                { label: <span className="tabular">{r.sessions.toLocaleString()}</span> },
                {
                  label:
                    r.unpriced_turns === r.turns ? (
                      <span className="text-text-muted">—</span>
                    ) : (
                      <span className="tabular">${r.cost_usd.toFixed(4)}</span>
                    ),
                },
              ],
            };
          })}
        />
      )}
    </section>
  );
}
