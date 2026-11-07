import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Table } from "@clickhouse/click-ui/Table";
import {
  listProjectionFailures,
  resolveProjectionFailures,
  retryProjectionFailures,
  type ProjectionFailure,
} from "../../api/maintenance";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { toast, toastError } from "../../components/Toast";
import { ListState } from "../../components/ui/ListState";
import { ListTime } from "../../components/ui/ListTime";
import { ListToolbar } from "../../components/ui/ListToolbar";
import { TableSkeleton } from "../../components/Skeleton";

// The server's default list page; a full page means there are more.
const FAILURE_LIST_LIMIT = 100;

/**
 * Projection failures: derived rows (an embedding, a graph edge, a summary) a write
 * could not produce. Retry runs the newest 50 again for this namespace; Dismiss marks one resolved
 * without retrying it. The ops are ORG_ADMIN.
 */
export function ProjectionFailuresPanel() {
  const identity = useIdentity();
  const [rows, setRows] = useState<ProjectionFailure[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const refresh = () => setReload((n) => n + 1);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setRows(null);
      listProjectionFailures(identity, { signal })
        .then((r) => live() && setRows(r.rows))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );

  const act = async (work: () => Promise<string>) => {
    setBusy(true);
    try {
      toast(await work(), "ok");
      refresh();
    } catch (e) {
      toastError("Projection failures", e);
    } finally {
      setBusy(false);
    }
  };
  const retry = () =>
    act(async () => {
      const r = await retryProjectionFailures(identity);
      return `Retried ${r.retried}: ${r.resolved} fixed, ${r.abandoned} given up`;
    });
  const dismiss = (id: string) =>
    act(async () => {
      await resolveProjectionFailures(identity.org, [id]);
      return "Dismissed";
    });

  if (error) return <ListState kind="error" things="projection failures" error={error} onRetry={refresh} />;
  if (rows?.length === 0)
    return (
      <ListState
        kind="empty"
        icon="check-in-circle"
        title="No projection failures"
        description="Every write here produced its embeddings, graph edges and summaries."
      />
    );
  return (
    <>
      <ListToolbar
        label="Projection failures"
        search={false}
        count={rows ? `${rows.length === FAILURE_LIST_LIMIT ? `${rows.length}+` : rows.length} open` : undefined}
      >
        <Button
          type="secondary"
          htmlType="button"
          iconLeft="refresh"
          disabled={busy || !rows}
          onClick={() => void retry()}
        >
          Retry failures
        </Button>
      </ListToolbar>
      <Table
        aria-label="Projection failures"
        aria-busy={rows === null || undefined}
        noDataMessage={rows === null ? <TableSkeleton label="Loading projection failures" /> : undefined}
        size="sm"
        headers={[
          { label: "Projection", width: "140px" },
          { label: "Memories" },
          { label: "Error" },
          { label: "When", width: "110px" },
          { label: <span className="sr-only">Actions</span>, width: "100px" },
        ]}
        rows={(rows ?? []).map((f) => ({
          id: f.failure_id,
          items: [
            { label: <span className="font-mono text-xs">{f.projection}</span> },
            {
              label: <span className="font-mono text-xs">{f.memory_keys.join(", ") || "—"}</span>,
              overflowMode: "wrap",
            },
            { label: <span className="text-xs">{f.error_message || f.error_type}</span>, overflowMode: "wrap" },
            { label: f.created_at ? <ListTime time={f.created_at} quiet /> : "—" },
            {
              label: (
                <Button
                  type="empty"
                  htmlType="button"
                  disabled={busy}
                  aria-label={`Dismiss failure ${f.failure_id}`}
                  onClick={() => void dismiss(f.failure_id)}
                >
                  Dismiss
                </Button>
              ),
            },
          ],
        }))}
      />
    </>
  );
}
