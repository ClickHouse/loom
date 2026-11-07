import { useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  REEMBED_LIMIT,
  SWEEP_LIMIT,
  expireAdapterFacts,
  reembedMemories,
  restoreNamespace,
  rollbackNamespace,
  snapshotNamespace,
  sweepExpired,
} from "../../api/maintenance";
import { useIdentity } from "../../lib/state";
import { saveJson } from "../../lib/download";
import { listTimeTitle } from "../../lib/format";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast, toastError } from "../../components/Toast";
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
      toastError("Repair failed", e);
    } finally {
      setBusy(false);
    }
  };

  const sweep = () =>
    run(async () => {
      const preview = await sweepExpired(identity, true);
      const due = preview.tombstoned + preview.demoted + preview.anonymized;
      if (due === 0) {
        const locked = preview.locked_skipped;
        const atLeast = preview.scanned === SWEEP_LIMIT ? " or more" : "";
        return toast(
          locked ? `${locked}${atLeast} memories are past their expiry but locked.` : "No memory is past its expiry.",
          "info",
        );
      }
      if (
        !(await confirmAction({
          title: `Apply the expiry of ${due} memories in ${identity.namespace} now?`,
          confirmLabel: "Apply",
        }))
      )
        return;
      const r = await sweepExpired(identity, false);
      const done = r.tombstoned + r.demoted + r.anonymized;
      // The pass counts before it writes: a scan or insert failure wrote nothing, and only
      // the search mirror's write comes after the memory rows landed.
      if (r.failures.some((f) => !f.startsWith("mirror")))
        return toast(`The expiry did not land: ${r.failures.join("; ")}`, "error");
      if (r.failures.length) return toast(`Expired ${done}; search may list some until the next pass`, "info");
      // A full pass may have left more; one that expired nothing only met locked rows.
      const rest = r.scanned === SWEEP_LIMIT && done > 0 ? "; run it again for the rest" : "";
      toast(`Expired ${done}; ${r.locked_skipped} locked kept${rest}`, "ok");
    });

  const reembed = () =>
    run(async () => {
      const ok = await confirmAction({
        title: `Re-embed the memories in ${identity.namespace} made by another embedding model?`,
        message: `Up to ${REEMBED_LIMIT} memories a run.`,
        confirmLabel: "Re-embed",
      });
      if (!ok) return;
      const r = await reembedMemories(identity);
      if (r.processed === 0) return toast(`No memory here needs re-embedding with ${r.target_model}.`, "info");
      const rest = r.processed === REEMBED_LIMIT ? "; run it again for the rest" : "";
      toast(`Re-embedded ${r.processed} with ${r.target_model}${rest}`, "ok");
    });

  const expire = () =>
    run(async () => {
      const name = adapter.trim();
      const ok = await confirmAction({
        title: `Remove the graph facts the ${name} extractor wrote in ${identity.namespace}?`,
        message: "The memories they came from stay.",
        confirmLabel: "Remove facts",
        danger: true,
      });
      if (!ok) return;
      const r = await expireAdapterFacts(identity, name);
      toast(`Removed ${r.expired_triples} facts and ${r.expired_triple_memories} memory links`, "ok");
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
        message: `${preview.reinstate} memories go back to their version then and ${preview.remove} created since are deleted${more}.`,
        confirmLabel: "Roll back",
        danger: true,
      });
      if (!ok) return;
      const r = await rollbackNamespace(identity, at, false);
      const rest = r.truncated ? "; run it again for the rest" : "";
      toast(`Rolled back: ${r.reinstate} reinstated, ${r.remove} removed${rest}`, "ok");
    });

  const download = () =>
    run(async () => {
      saveJson(await snapshotNamespace(identity), `${identity.namespace}-snapshot.json`);
    });

  const restore = (file: File) =>
    run(async () => {
      const payload: unknown = JSON.parse(await file.text());
      const ok = await confirmAction({
        title: `Restore ${file.name} into ${identity.namespace}?`,
        message: restoreWarning(payload, identity),
        confirmLabel: "Restore",
        danger: true,
      });
      if (!ok) return;
      const r = await restoreNamespace(identity.org, identity.namespace, payload);
      if (!r.restored) return toast("This snapshot was already restored here", "info");
      const erased = r.counts.erased_skipped ? `, ${r.counts.erased_skipped} skipped as erased` : "";
      // `memories` is the file's count; the erased ones in it were not written.
      toast(`Restored ${r.counts.memories - r.counts.erased_skipped} memories${erased}`, "ok");
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

/** What a restore of `payload` does here: a snapshot of another namespace (or another
 *  org's) adds its memories and its members' grants; one of this namespace puts it back as
 *  it was. */
function restoreWarning(payload: unknown, here: { org: string; namespace: string }): string {
  const p = (typeof payload === "object" && payload !== null ? payload : {}) as {
    org?: string;
    source_namespace?: string;
    created_at?: string;
    memory_count?: number;
  };
  const when = listTimeTitle(p.created_at) || "at an unknown time";
  const taken = `It holds ${p.memory_count ?? "an unknown number of"} memories, taken ${when}.`;
  const otherOrg = !!p.org && p.org !== here.org;
  if (otherOrg || (p.source_namespace && p.source_namespace !== here.namespace)) {
    const name = [otherOrg ? p.org : "", p.source_namespace ?? ""].filter(Boolean).join("/");
    return `${taken} It is a snapshot of ${name}: its memories are written here, and its members get the grants they had there.`;
  }
  return `${taken} Its memories replace their current versions here, deleted ones come back, and its grants are active again, including any revoked since.`;
}
