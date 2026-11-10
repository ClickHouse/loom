import { useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { approveDataset, DATASET_TYPES, getDatasets, type Dataset, type DatasetType, type Identity } from "../api";
import { ApiError } from "../lib/errors";
import { humanizeError } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { toast, toastError } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, Loading, SectionHeading } from "../components/ui";

const SCHEMA_STATE: Record<string, string> = {
  inferred: "types inferred",
  needs_clarification: "needs a decision",
  approved: "approved",
};

/**
 * The typed tables a document's file became (knowledge.dataset_get): each one's columns
 * and types, what the typing asks a person, and an Approve that may retype columns
 * (knowledge.dataset_approve). `canApprove` follows the writer role the op asks.
 */
export function DatasetsDialog({
  identity,
  sourceUri,
  canApprove,
  onClose,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  sourceUri: string;
  canApprove: boolean;
  onClose: () => void;
}) {
  // null while it reads; `found: false` when the document is no longer live.
  const [read, setRead] = useState<{ found: boolean; datasets: Dataset[] } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reload, setReload] = useState(0);
  useAbortableEffect(
    (signal, live) => {
      getDatasets(identity, sourceUri, { signal })
        .then((r) => live() && setRead(r))
        .catch((e) => live() && setError(e));
    },
    [identity.org, identity.namespace, sourceUri, reload],
  );
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title="Tables" className="w-full max-w-[720px]">
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!error && read === null && <Loading label="Loading the tables" />}
      {read && !read.found && <p className="text-sm text-text-muted">This document is no longer here.</p>}
      {read?.found && read.datasets.length === 0 && (
        <p className="text-sm text-text-muted">No table was read from this document.</p>
      )}
      <div className="space-y-6">
        {read?.datasets.map((d) => (
          <DatasetCard
            key={d.dataset_id}
            identity={identity}
            sourceUri={sourceUri}
            dataset={d}
            canApprove={canApprove}
            onChanged={() => setReload((n) => n + 1)}
          />
        ))}
      </div>
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}

function DatasetCard({
  identity,
  sourceUri,
  dataset: d,
  canApprove,
  onChanged,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  sourceUri: string;
  dataset: Dataset;
  canApprove: boolean;
  onChanged: () => void;
}) {
  const [types, setTypes] = useState<Record<string, DatasetType>>({});
  const [busy, setBusy] = useState(false);
  const retyped = Object.fromEntries(
    Object.entries(types).filter(([name, t]) => d.columns.find((c) => c.name === name)?.type !== t),
  );
  const approve = async () => {
    setBusy(true);
    try {
      const r = await approveDataset(identity, sourceUri, d, retyped);
      toast(r.found ? `Approved ${d.name}` : `${d.name} is no longer in this document`, r.found ? "ok" : "info");
      onChanged();
    } catch (e) {
      // A newer revision or approval moved the table on: show it as it is now.
      if (e instanceof ApiError && e.code === "PRECONDITION_FAILED") {
        toast(`${d.name} changed meanwhile; here it is as it is now`, "info");
        onChanged();
      } else toastError("Approve failed", e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section>
      <SectionHeading
        label={d.name || d.sheet || "Table"}
        level={3}
        caption={`${d.row_count.toLocaleString()} rows${d.rejected_rows ? `, ${d.rejected_rows} rejected` : ""}`}
        actions={
          <Badge
            text={d.approved_by ? `approved by ${d.approved_by}` : (SCHEMA_STATE[d.schema_state] ?? d.schema_state)}
            state="neutral"
            size="sm"
          />
        }
      />
      {d.clarifications.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm">
          {d.clarifications.map((c) => (
            <li key={`${c.code}-${c.column}`}>
              {c.column && <span className="font-mono text-xs">{c.column}: </span>}
              {c.detail}
            </li>
          ))}
        </ul>
      )}
      <ul className="mt-2 divide-y divide-border">
        {d.columns.map((c) => (
          <li key={c.name} className="flex items-center gap-3 py-1.5">
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{c.name}</span>
            {c.inferred !== c.type && <span className="text-xs text-text-muted">read as {c.inferred}</span>}
            {canApprove ? (
              <div className="w-40">
                <Select
                  value={types[c.name] ?? c.type}
                  onSelect={(v) => setTypes((t) => ({ ...t, [c.name]: v as DatasetType }))}
                  triggerProps={{ "aria-label": `Type of ${c.name}` }}
                >
                  {DATASET_TYPES.map((t) => (
                    <Select.Item key={t} value={t}>
                      {t}
                    </Select.Item>
                  ))}
                </Select>
              </div>
            ) : (
              <span className="text-xs text-text-muted">{c.type}</span>
            )}
          </li>
        ))}
      </ul>
      {canApprove && (d.schema_state !== "approved" || Object.keys(retyped).length > 0) && (
        <div className="mt-2 flex justify-end">
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void approve()}>
            {Object.keys(retyped).length ? "Retype and approve" : "Approve schema"}
          </Button>
        </div>
      )}
    </section>
  );
}
