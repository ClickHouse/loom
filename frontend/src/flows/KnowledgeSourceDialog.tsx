import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  addKnowledgeSource,
  checkKnowledgeSource,
  removeKnowledgeSource,
  rotateKnowledgeSource,
  syncKnowledgeSourceNow,
} from "../api/knowledgeSources";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import {
  credentialLabel,
  emptySourceForm,
  KIND_FORM,
  normalisedName,
  probeCount,
  retiredMessage,
  sourceFormFingerprint,
  sourceFormProblems,
  sourceFormTouched,
  type KnowledgeSourceCheckResult,
  type KnowledgeSourceProbe,
  type SourceForm,
} from "../lib/knowledgeSources";
import { toast } from "../components/Toast";
import { Alert } from "../components/ui/Alert";
import { Loading } from "../components/ui/Loading";
import { CancelButton, DialogFrame, ErrorNote } from "./TaskDialogFrame";

interface DialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
}

/** What a probe found: the count and up to five titles. */
function ProbeResult({ probe, lead }: { probe: KnowledgeSourceProbe; lead: string }) {
  return (
    <div className="text-sm space-y-1" role="status">
      <p>
        {lead} {probeCount(probe)}.
      </p>
      {probe.sample.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-text-muted" aria-label="Sample documents">
          {probe.sample.map((t, i) => (
            // Titles can repeat, so the position is the key.
            <li key={i} className="break-words">
              {t}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type SetField = <K extends keyof SourceForm>(key: K, value: SourceForm[K]) => void;

export function AddRecordingSourceDialog({
  open,
  onOpenChange,
  org,
  namespace,
  label,
  onAdded,
}: DialogProps & { label: string; onAdded: (name: string) => void }) {
  const [form, setForm] = useState<SourceForm>(emptySourceForm);
  const [checked, setChecked] = useState<{ fingerprint: string; probe: KnowledgeSourceProbe } | null>(null);
  const [busy, setBusy] = useState<"check" | "add" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const set: SetField = (key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
  };
  const problems = sourceFormProblems(form);
  const fingerprint = sourceFormFingerprint(form);
  const passed = checked !== null && checked.fingerprint === fingerprint;

  const run = async (dryRun: boolean) => {
    setBusy(dryRun ? "check" : "add");
    setError(null);
    try {
      const out = await addKnowledgeSource({ org, namespace }, form, dryRun, "explicit");
      if (dryRun) setChecked({ fingerprint, probe: out.probe });
      else {
        onAdded(out.name);
        onOpenChange(false);
      }
    } catch (e) {
      // A failed add keeps the check that passed, so Add can be tried again.
      if (dryRun) setChecked(null);
      setError(humanizeError(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={560}
      title={`Add a recording source · ${label}`}
      description={KIND_FORM[form.kind].about}
      footer={
        <>
          <CancelButton />
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "check"}
            disabled={busy !== null || problems.length > 0}
            onClick={() => void run(true)}
          >
            Check
          </Button>
          <Button
            type="primary"
            htmlType="button"
            loading={busy === "add"}
            disabled={busy !== null || !passed}
            onClick={() => void run(false)}
          >
            Add source
          </Button>
        </>
      }
    >
      <TextField
        label="Name (2-32 lower-case letters, digits, - or _, starting with a letter)"
        value={form.name}
        autoComplete="off"
        placeholder="design-docs"
        onChange={(v) => set("name", v)}
        autoFocus
      />
      <TextAreaField label="Meetings (meeting ids, one per line)" value={form.meetings} rows={3} onChange={(v) => set("meetings", v)} />
      <PasswordField
        label={credentialLabel(form.kind)}
        value={form.credential}
        autoComplete="new-password"
        onChange={(v) => set("credential", v)}
      />
      {problems.length > 0 && sourceFormTouched(form) && (
        <ul className="text-xs text-text-muted list-disc pl-5" aria-label="Before checking">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {checked && passed && (
        <ProbeResult probe={checked.probe} lead={`${KIND_FORM[form.kind].reads}: ${normalisedName(form.name)} lists`} />
      )}
      {checked && !passed && <p className="text-xs text-text-muted">The form changed since the check: check again.</p>}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function CheckSourceDialog({ open, onOpenChange, org, namespace, name }: DialogProps & { name: string }) {
  const [result, setResult] = useState<KnowledgeSourceCheckResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useAbortableEffect(
    async (signal, live) => {
      setResult(null);
      setError(null);
      try {
        const r = await checkKnowledgeSource({ org, namespace }, name, { signal });
        if (live()) setResult(r);
      } catch (e) {
        if (live()) setError(humanizeError(e));
      }
    },
    [org, namespace, name, nonce],
  );
  const running = result === null && error === null;
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      title={`Check ${name}`}
      description="The stored credential lists the source now; nothing is written."
      footer={
        <>
          <CancelButton label="Close" />
          <Button type="secondary" htmlType="button" disabled={running} onClick={() => setNonce((n) => n + 1)}>
            Check again
          </Button>
        </>
      }
    >
      {running && <Loading layout="inline" label="Checking the source" />}
      {result?.reachable && <ProbeResult probe={result} lead="Reachable: it lists" />}
      {result && !result.reachable && <Alert state="warning" text={result.summary || "The source is unreachable."} />}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function SyncNowDialog({
  open,
  onOpenChange,
  org,
  namespace,
  name,
  onRequested,
}: DialogProps & { name: string; onRequested: () => void }) {
  const [full, setFull] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await syncKnowledgeSourceNow({ org, namespace }, name, full, "explicit");
      toast(
        out.enabled
          ? `Sync requested: ${name} is synced next`
          : `Sync requested; sync is off on this deployment, so it waits until it is on`,
        "ok",
      );
      onRequested();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      title={`Sync ${name} now`}
      description="Records a request: this source is the next one Loom syncs. Nothing is fetched while you wait."
      footer={
        <>
          <CancelButton />
          <Button type="primary" htmlType="button" loading={busy} onClick={() => void submit()}>
            Request sync
          </Button>
        </>
      }
    >
      <Checkbox
        label="List every document, not only those changed since the last sync"
        checked={full}
        onCheckedChange={(v) => setFull(v === true)}
      />
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function RotateSourceDialog({
  open,
  onOpenChange,
  org,
  namespace,
  name,
  kind,
  onRotated,
}: DialogProps & { name: string; kind: string; onRotated: () => void }) {
  const [credential, setCredential] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await rotateKnowledgeSource({ org, namespace }, name, credential.trim(), "explicit");
      onRotated();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      title={`Rotate the credential · ${name}`}
      description="The new key is checked against the source before it replaces the old one; a key that fails changes nothing."
      footer={
        <>
          <CancelButton />
          <Button
            type="primary"
            htmlType="button"
            loading={busy}
            disabled={!credential.trim()}
            onClick={() => void submit()}
          >
            Rotate
          </Button>
        </>
      }
    >
      <PasswordField
        label={credentialLabel(kind)}
        value={credential}
        autoComplete="new-password"
        onChange={setCredential}
        autoFocus
      />
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function RetireSourceDialog({
  open,
  onOpenChange,
  org,
  namespace,
  name,
  enabled,
  onRetired,
}: DialogProps & { name: string; enabled: boolean; onRetired: () => void }) {
  const [typed, setTyped] = useState("");
  const [deleteDocuments, setDeleteDocuments] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await removeKnowledgeSource({ org, namespace }, name, typed.trim(), deleteDocuments, "explicit");
      toast(retiredMessage(name, deleteDocuments, out.documents_pending, enabled), "ok");
      onRetired();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={460}
      title={`Retire ${name}?`}
      description="Loom stops pulling from the source and clears its stored credential. The documents it wrote stay unless you ask for them to go too."
      footer={
        <>
          <CancelButton />
          <Button
            type="danger"
            htmlType="button"
            loading={busy}
            disabled={typed.trim() !== name}
            onClick={() => void submit()}
          >
            Retire source
          </Button>
        </>
      }
    >
      <Checkbox
        label="Also retire the documents it wrote"
        checked={deleteDocuments}
        onCheckedChange={(v) => setDeleteDocuments(v === true)}
      />
      <TextField
        label={
          <>
            Type <span className="font-mono">{name}</span> to confirm
          </>
        }
        value={typed}
        autoComplete="off"
        onChange={setTyped}
        autoFocus
      />
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}
