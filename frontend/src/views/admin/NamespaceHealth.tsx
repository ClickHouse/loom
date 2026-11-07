import { useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Table } from "@clickhouse/click-ui/Table";
import {
  getCalibration,
  getExtractionBatches,
  getMemoryHealth,
  type Calibration,
  type ExtractionBatch,
  type MemoryHealth,
} from "../../api/maintenance";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { AgentBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { Loading, MetaList, MetaRow, Panel, SectionHeading, Stat } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";

/**
 * One namespace's own health: how much of what it stores is ever recalled
 * (memory.health), the similarity gates its searches run under (calibration.get), and
 * the transcripts still waiting for extraction (memory.extraction_status).
 */
export function NamespaceHealthPanel() {
  const identity = useIdentity();
  const [health, setHealth] = useState<MemoryHealth | null>(null);
  const [calibration, setCalibration] = useState<Calibration | null>(null);
  const [batches, setBatches] = useState<{ rows: ExtractionBatch[]; pending: number; failed: number } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);

  useAbortableEffect(
    (signal, live) => {
      setError(null);
      setHealth(null);
      Promise.all([
        getMemoryHealth(identity, { signal }),
        getCalibration(identity, { signal }),
        getExtractionBatches(identity, { signal }),
      ])
        .then(([h, c, b]) => {
          if (!live()) return;
          setHealth(h);
          setCalibration(c);
          setBatches({ rows: b.batches ?? [], pending: b.pending, failed: b.failed });
        })
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, reload],
  );

  if (error)
    return <ListState kind="error" things="namespace health" error={error} onRetry={() => setReload((n) => n + 1)} />;
  if (!health || !calibration || !batches) return <Loading label="Loading namespace health" />;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Live memories" value={health.live.toLocaleString()} />
        <Stat label="Ever recalled" value={health.recalled_ever.toLocaleString()} />
        <Stat
          label="Silent"
          value={`${Math.round(health.silent_share * 100)}%`}
          hint={`not recalled in ${health.silent_after_days} days`}
        />
        <Stat label="Expiring in 7 days" value={health.staleness.expiring_7d.toLocaleString()} />
      </div>
      <Panel>
        <SectionHeading label="Search gates" level={2} caption={`From the ${calibration.source} calibration.`} />
        <MetaList className="mt-2">
          <MetaRow label="Embedder" mono>
            {calibration.embedder_model || "—"}
          </MetaRow>
          <MetaRow label="Noise floor">{calibration.noise_floor.toFixed(3)}</MetaRow>
          <MetaRow label="Abstain below">{calibration.abstain_threshold.toFixed(3)}</MetaRow>
          <MetaRow label="Conflict above">{calibration.conflict_threshold.toFixed(3)}</MetaRow>
          <MetaRow label="Duplicate above">{calibration.duplicate_threshold.toFixed(3)}</MetaRow>
        </MetaList>
      </Panel>
      <section>
        <SectionHeading
          label="Waiting extractions"
          level={2}
          caption={`${batches.pending} pending · ${batches.failed} failed`}
        />
        {batches.rows.length === 0 ? (
          <p className="text-sm text-text-muted">No transcript is waiting for extraction.</p>
        ) : (
          <Table
            aria-label="Waiting extractions"
            size="sm"
            headers={[
              { label: "Agent" },
              { label: "Status", width: "120px" },
              { label: "Messages", width: "100px" },
              { label: "Written", width: "90px" },
              { label: "Queued", width: "110px" },
            ]}
            rows={batches.rows.map((b) => ({
              id: b.queue_id,
              items: [
                { label: <AgentBadge agent={b.agent} /> },
                {
                  label: (
                    <Badge
                      text={b.status}
                      state={b.status === "failed" ? "danger" : b.status === "done" ? "success" : "neutral"}
                      size="sm"
                    />
                  ),
                },
                { label: <span className="tabular">{b.message_count}</span> },
                { label: <span className="tabular">{b.written}</span> },
                { label: b.enqueued_at ? <ListTime time={b.enqueued_at} quiet /> : "—" },
              ],
            }))}
          />
        )}
      </section>
    </div>
  );
}
