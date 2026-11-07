import { useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Table } from "@clickhouse/click-ui/Table";
import {
  EXTRACTION_LIMIT,
  getCalibration,
  getExtractionBatches,
  getMemoryHealth,
  type Calibration,
  type ExtractionBatches,
  type MemoryHealth,
} from "../../api/maintenance";
import { useIdentity } from "../../lib/state";
import { useAbortableEffect } from "../../lib/hooks";
import { AgentBadge } from "../../components/AgentBadge";
import { ListState } from "../../components/ui/ListState";
import { Loading, MetaList, MetaRow, Panel, SectionHeading, Stat } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";

// What each calibration tier is, for the caption over the gates.
const CALIBRATION_FROM: Record<string, string> = {
  namespace: "this namespace's calibration",
  cluster: "the cluster's calibration",
  scalar: "the default settings",
};

type Read<T> = { value: T } | { error: unknown } | null;

/** One read per section, so a failed one leaves the others on screen. */
function useRead<T>(read: (signal: AbortSignal) => Promise<T>, deps: unknown[]): [Read<T>, () => void] {
  const [state, setState] = useState<Read<T>>(null);
  const [reload, setReload] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      setState(null);
      read(signal)
        .then((value) => live() && setState({ value }))
        .catch((error) => live() && setState({ error }));
    },
    [...deps, reload],
  );
  return [state, () => setReload((n) => n + 1)];
}

/**
 * One namespace's own health: how much of what it stores is ever recalled
 * (memory.health), the similarity gates its searches run under (calibration.get), and
 * its newest extraction batches (memory.extraction_status).
 */
export function NamespaceHealthPanel() {
  const identity = useIdentity();
  const deps = [identity.org, identity.namespace];
  const [health, rereadHealth] = useRead((signal) => getMemoryHealth(identity, { signal }), deps);
  const [calibration, rereadCalibration] = useRead((signal) => getCalibration(identity, { signal }), deps);
  const [batches, rereadBatches] = useRead((signal) => getExtractionBatches(identity, { signal }), deps);

  return (
    <div className="space-y-6">
      {section(health, "memory report", rereadHealth, (h) => (
        <MemoryReport health={h} />
      ))}
      {section(calibration, "search gates", rereadCalibration, (c) => (
        <SearchGates calibration={c} />
      ))}
      {section(batches, "extraction batches", rereadBatches, (b) => (
        <Extractions batches={b} />
      ))}
    </div>
  );
}

function section<T>(read: Read<T>, things: string, retry: () => void, show: (value: T) => React.ReactNode) {
  if (read === null) return <Loading label={`Loading ${things}`} />;
  if ("error" in read) return <ListState kind="error" things={things} error={read.error} onRetry={retry} />;
  return show(read.value);
}

function MemoryReport({ health }: { health: MemoryHealth }) {
  return (
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
  );
}

function SearchGates({ calibration }: { calibration: Calibration }) {
  const from = CALIBRATION_FROM[calibration.source] ?? `the ${calibration.source} calibration`;
  return (
    <Panel>
      <SectionHeading label="Search gates" level={2} caption={`From ${from}.`} />
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
  );
}

function Extractions({ batches }: { batches: ExtractionBatches }) {
  return (
    <section>
      <SectionHeading
        label="Recent extractions"
        level={2}
        caption={`${batchCount(batches.batches.length)}, ${batches.failed} failed; ${batches.pending} waiting in all`}
      />
      {batches.batches.length === 0 ? (
        <p className="text-sm text-text-muted">No transcript was queued for extraction.</p>
      ) : (
        <Table
          aria-label="Recent extractions"
          size="sm"
          headers={[
            { label: "Agent" },
            { label: "Status", width: "120px" },
            { label: "Messages", width: "100px" },
            { label: "Written", width: "90px" },
            { label: "Queued", width: "110px" },
          ]}
          rows={batches.batches.map((b) => ({
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
  );
}

function batchCount(n: number): string {
  if (n === EXTRACTION_LIMIT) return `The newest ${n}`;
  return n === 1 ? "1 batch" : `${n} batches`;
}
