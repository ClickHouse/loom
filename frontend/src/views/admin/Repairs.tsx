import { useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  expireAdapterFacts,
  reembedMemories,
  restoreNamespace,
  rollbackNamespace,
  snapshotNamespace,
  sweepExpired,
} from "../../api/maintenance";
import { useIdentity } from "../../lib/state";
import { humanizeError } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { Panel, SectionHeading } from "../../components/ui";
import { DateTimeField } from "../../flows/TaskDialogFrame";

/**
 * A namespace admin's repairs. Each one that can count first (the expiry sweep, a
 * rollback) shows what it would do and asks before it writes; the others ask first.
 */
export function RepairsPanel() {
  const identity = useIdentity();
  const [busy, setBusy] = useState(false);
  const [adapter, setAdapter] = useState("");
  const [asOf, setAsOf] = useState("");
  const restoreInput = useRef<HTMLInputElement>(null);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    try {
      await work();
    } catch (e) {
      toast(`Failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  const sweep = () =>
    run(async () => {
      const preview = await sweepExpired(identity, true);
      const due = preview.tombstoned + preview.demoted + preview.anonymized;
      if (due === 0) return toast("No memory is past its expiry.", "info");
      if (!(await confirmAction({ title: `Apply the expiry of ${due} memories now?`, confirmLabel: "Apply" }))) return;
      const r = await sweepExpired(identity, false);
      toast(`Expired ${r.tombstoned + r.demoted + r.anonymized}; ${r.locked_skipped} locked kept`, "ok");
    });

  const reembed = () =>
    run(async () => {
      const ok = await confirmAction({
        title: "Re-embed memories made by another embedding model?",
        message: "Up to 200 a run; each is one embedding call.",
        confirmLabel: "Re-embed",
      });
      if (!ok) return;
      const r = await reembedMemories(identity);
      toast(`Re-embedded ${r.processed} with ${r.target_model}; ${r.skipped_already_current} already current`, "ok");
    });

  const expire = () =>
    run(async () => {
      const name = adapter.trim();
      const ok = await confirmAction({
        title: `Remove the graph facts the ${name} extractor wrote?`,
        message: "The memories they came from stay.",
        confirmLabel: "Remove facts",
        danger: true,
      });
      if (!ok) return;
      const r = await expireAdapterFacts(identity, name);
      toast(`Removed ${r.expired_triples} facts from ${r.expired_triple_memories} memories`, "ok");
      setAdapter("");
    });

  const rollback = () =>
    run(async () => {
      const at = new Date(asOf).toISOString();
      const preview = await rollbackNamespace(identity, at, true);
      if (preview.reinstate + preview.remove === 0) return toast("Every memory already stands as it did then.", "info");
      const more = preview.truncated ? " (the first 5,000 keys)" : "";
      const ok = await confirmAction({
        title: `Roll ${identity.namespace} back to ${asOf.replace("T", " ")}?`,
        message: `${preview.reinstate} memories go back to their version then and ${preview.remove} written since are deleted${more}.`,
        confirmLabel: "Roll back",
        danger: true,
      });
      if (!ok) return;
      const r = await rollbackNamespace(identity, at, false);
      toast(`Rolled back: ${r.reinstate} reinstated, ${r.remove} removed`, "ok");
    });

  const download = () =>
    run(async () => {
      const snapshot = await snapshotNamespace(identity);
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `${identity.namespace}-snapshot.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });

  const restore = (file: File) =>
    run(async () => {
      const payload: unknown = JSON.parse(await file.text());
      const ok = await confirmAction({
        title: `Restore ${file.name} into ${identity.namespace}?`,
        message: "Its memories, types and grants are written here; nothing here is deleted.",
        confirmLabel: "Restore",
      });
      if (!ok) return;
      const r = await restoreNamespace(identity.org, identity.namespace, payload);
      toast(
        r.restored ? `Restored ${r.counts.memories ?? 0} memories` : "This snapshot was already restored here",
        "ok",
      );
    });

  return (
    <div className="space-y-4">
      <Panel>
        <SectionHeading label="Expiry and embeddings" level={2} />
        <div className="mt-2 flex flex-wrap gap-2">
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void sweep()}>
            Apply expiries now
          </Button>
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => void reembed()}>
            Re-embed stale vectors
          </Button>
        </div>
      </Panel>
      <Panel>
        <SectionHeading
          label="Remove an extractor's facts"
          level={2}
          caption="For an extractor that wrote wrong graph facts; the memories stay."
        />
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <TextField id="repair-adapter" label="Extractor" value={adapter} onChange={setAdapter} />
          <Button type="danger" htmlType="button" disabled={busy || !adapter.trim()} onClick={() => void expire()}>
            Remove facts
          </Button>
        </div>
      </Panel>
      <Panel>
        <SectionHeading label="Roll back" level={2} caption="Every memory back to how it stood at a time." />
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <DateTimeField id="repair-as-of" label="As of" value={asOf} onChange={setAsOf} />
          <Button type="danger" htmlType="button" disabled={busy || !asOf} onClick={() => void rollback()}>
            Roll back
          </Button>
        </div>
      </Panel>
      <Panel>
        <SectionHeading label="Snapshot" level={2} caption="The namespace's memories, types and grants as one file." />
        <div className="mt-2 flex flex-wrap gap-2">
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="download"
            disabled={busy}
            onClick={() => void download()}
          >
            Download snapshot
          </Button>
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="upload"
            disabled={busy}
            onClick={() => restoreInput.current?.click()}
          >
            Restore a snapshot
          </Button>
          <input
            ref={restoreInput}
            type="file"
            accept="application/json,.json"
            className="hidden"
            aria-label="Snapshot file"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void restore(file);
            }}
          />
        </div>
      </Panel>
    </div>
  );
}
