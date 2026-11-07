import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { inspectKnowledgeSource } from "../api/knowledgeSources";
import { kindLabel, syncBadge, type KnowledgeSourceDetail } from "../lib/knowledgeSources";
import { humanizeError } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, ListTime, Loading, MetaList, MetaRow, SectionHeading } from "../components/ui";

/** One document source as knowledge.source.inspect answers it: its settings, its last
 *  sync and the documents it wrote. */
export function SourceDetailsDialog({
  org,
  namespace,
  name,
  onClose,
}: {
  org: string;
  namespace: string;
  name: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<KnowledgeSourceDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  useAbortableEffect(
    (signal, live) => {
      inspectKnowledgeSource({ org, namespace }, name, { signal })
        .then((d) => live() && setDetail(d))
        .catch((e) => live() && setError(e));
    },
    [org, namespace, name],
  );
  return (
    <Modal open onOpenChange={(v) => !v && onClose()} title={name} className="w-full max-w-[640px]">
      {!!error && <Alert role="alert" state="danger" text={humanizeError(error)} />}
      {!error && detail === null && <Loading label="Loading the source" />}
      {detail && (
        <div className="space-y-4">
          <MetaList>
            <MetaRow label="Kind">{kindLabel(detail.kind)}</MetaRow>
            {Object.entries(detail.config).map(([k, v]) => (
              <MetaRow key={k} label={k} mono>
                {configText(v)}
              </MetaRow>
            ))}
            <MetaRow label="Added by">{detail.created_by || "—"}</MetaRow>
            <MetaRow label="Last sync">
              {detail.sync.last_synced_at ? <ListTime time={detail.sync.last_synced_at} /> : "never"} ·{" "}
              {syncBadge(detail.sync).text}
            </MetaRow>
            {/* A failed turn's error, or why an ok turn stopped early. */}
            {detail.sync.error && (
              <MetaRow
                label={detail.sync.status === "ok" ? "Note" : "Error"}
                tone={detail.sync.status === "ok" ? undefined : "danger"}
              >
                {detail.sync.error}
              </MetaRow>
            )}
          </MetaList>
          <section>
            <SectionHeading label="Documents" level={3} count={detail.documents.length} />
            {detail.documents.length === 0 ? (
              <p className="text-sm text-text-muted">No document yet.</p>
            ) : (
              <ul className="mt-1 max-h-64 overflow-y-auto divide-y divide-border">
                {detail.documents.map((d) => (
                  <li key={d.source_uri} className="flex items-center gap-2 py-1.5 text-xs">
                    <span className="min-w-0 flex-1 truncate">{d.title || d.source_uri}</span>
                    <ListTime time={d.ingested_at} quiet />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
      <ModalFooter>
        <Button type="secondary" htmlType="button" onClick={onClose}>
          Close
        </Button>
      </ModalFooter>
    </Modal>
  );
}

/** A config value as text: a list of ids reads as one comma-separated line. */
function configText(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ");
  return typeof v === "string" ? v : JSON.stringify(v);
}
