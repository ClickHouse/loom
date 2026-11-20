/**
 * Adding, checking, syncing, rotating and retiring a namespace's document source.
 * Loaded on first use, behind `lazy()`, by the Sources list (a repository card's, or the
 * Library's).
 *
 * Check probes the selected links or IDs before Add stores a source. Own connected
 * accounts use the server's caller binding. Shared credentials remain in dialog
 * memory, clear when their provider or account mode changes, and are never displayed.
 */
import { useEffect, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { PasswordField } from "@clickhouse/click-ui/PasswordField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  addKnowledgeSource,
  checkKnowledgeSource,
  removeKnowledgeSource,
  rotateKnowledgeSource,
  syncKnowledgeSourceNow,
  parseKnowledgeSourceLinks,
  type KnowledgeSourceParsedLink,
} from "../api/knowledgeSources";
import { committedAnyway, humanizeError } from "../lib/errorWords";
import { canCallOwnConnections } from "../lib/canCall";
import { useStore } from "../lib/state";
import { useAbortableEffect, useMounted } from "../lib/hooks";
import { useSourceAccount } from "./SourceAccountField";
import { SourceRegistrationFields, type SetField } from "./SourceRegistrationFields";
import { SourceLinkPreview, validateSourceLinkPreview } from "./SourceLinkPreview";
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
  sourceLinks,
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
function ProbeResult({
  probe,
  lead,
  connected = false,
}: {
  probe: KnowledgeSourceProbe;
  lead: string;
  connected?: boolean;
}) {
  return (
    <div className="text-sm space-y-1" role="status">
      <p>
        {connected
          ? `Access checked for ${probe.checked ?? 0}${probe.sampled ? " sampled" : ""} items. This check does not import documents.`
          : `${lead} ${probeCount(probe)}.`}
      </p>
      {!connected && probe.sample.length > 0 && (
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

type AddSourceProps = DialogProps & { label: string; onAdded: (name: string) => void; allowShared?: boolean };

export function AddSourceDialog(props: AddSourceProps) {
  const scope = useStore((s) =>
    JSON.stringify([
      s.identity?.org,
      s.whoami?.org,
      s.whoami?.subject,
      s.whoami?.roles,
      s.whoami?.is_service_account,
      s.authed,
      s.sessionExpired,
    ]),
  );
  return <SourceRegistration key={JSON.stringify([props.org, props.namespace, props.allowShared, scope])} {...props} />;
}

function SourceRegistration({
  open,
  onOpenChange,
  org,
  namespace,
  label,
  onAdded,
  allowShared = true,
}: AddSourceProps) {
  const whoami = useStore((s) => s.whoami);
  const mounted = useMounted();
  const actor = JSON.stringify([
    useStore.getState().identity?.org,
    whoami?.org,
    whoami?.subject,
    whoami?.roles,
    whoami?.is_service_account,
    useStore.getState().authed,
    useStore.getState().sessionExpired,
  ]);
  const current = () => {
    const s = useStore.getState();
    return (
      mounted.current &&
      actor ===
        JSON.stringify([
          s.identity?.org,
          s.whoami?.org,
          s.whoami?.subject,
          s.whoami?.roles,
          s.whoami?.is_service_account,
          s.authed,
          s.sessionExpired,
        ])
    );
  };
  const running = useRef(false);
  const previewRequest = useRef<AbortController | null>(null);
  useEffect(() => () => previewRequest.current?.abort(), []);
  const [form, setForm] = useState<SourceForm>(() => {
    const f = emptySourceForm();
    return !allowShared || (whoami?.org === org && canCallOwnConnections(whoami))
      ? { ...f, credentialMode: "connected", inputMode: "links" }
      : f;
  });
  const connected = form.credentialMode === "connected";
  const account = useSourceAccount(org, form.kind === "recording" ? null : form.kind, connected);
  const [checked, setChecked] = useState<{ fingerprint: string; probe: KnowledgeSourceProbe } | null>(null);
  const [preview, setPreview] = useState<{
    fingerprint: string;
    links: KnowledgeSourceParsedLink[];
    valid: boolean;
  } | null>(null);
  const [busy, setBusy] = useState<"check" | "add" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const set: SetField = (key, value) => {
    // The credential and last error belong to the kind they were entered for.
    const switched = (key === "kind" && value !== form.kind) || key === "credentialMode";
    if (switched) setError(null);
    setForm((f) => ({
      ...f,
      [key]: value,
      ...(switched ? { credential: "" } : {}),
      ...(key === "kind" && value === "recording" ? { credentialMode: "shared" } : {}),
    }));
  };
  const problems = sourceFormProblems(form);
  const fingerprint = JSON.stringify([
    org,
    namespace,
    whoami?.subject,
    sourceFormFingerprint(form),
    connected ? account.stamp : "shared",
  ]);
  const accountReady = !connected || account.ready;
  const currentFingerprint = useRef(fingerprint);
  currentFingerprint.current = fingerprint;
  const usesLinks = form.inputMode === "links" && form.kind !== "recording";
  const previewReady = !usesLinks || (preview?.fingerprint === fingerprint && preview.valid);
  const passed = checked !== null && checked.fingerprint === fingerprint && accountReady && previewReady;

  const run = async (dryRun: boolean) => {
    if (!current() || running.current || problems.length || !accountReady || (!dryRun && !passed)) return;
    running.current = true;
    setBusy(dryRun ? "check" : "add");
    setError(null);
    const controller = new AbortController();
    if (dryRun) {
      setChecked(null);
      setPreview(null);
    }
    try {
      if (dryRun && usesLinks) {
        const selected = sourceLinks(form.links);
        previewRequest.current = controller;
        const parsed = await parseKnowledgeSourceLinks({ org, namespace }, selected, { signal: controller.signal });
        if (!current() || currentFingerprint.current !== fingerprint) return;
        const { matches, problems } = validateSourceLinkPreview(parsed.links, form.kind, selected);
        if (matches) setPreview({ fingerprint, links: parsed.links, valid: problems.length === 0 });
        if (problems.length) {
          setError(problems.join(" "));
          return;
        }
      }
      const out = await addKnowledgeSource({ org, namespace }, form, dryRun);
      if (!current() || (dryRun && currentFingerprint.current !== fingerprint)) return;
      if (dryRun) setChecked({ fingerprint, probe: out.probe });
      else {
        onAdded(out.name);
        onOpenChange(false);
      }
    } catch (e) {
      if (!current() || (dryRun && currentFingerprint.current !== fingerprint)) return;
      if (!dryRun && committedAnyway("Add source", e)) {
        onAdded(normalisedName(form.name));
        onOpenChange(false);
        return;
      }
      // A refused add preserves the draft; it never switches account or content.
      if (dryRun) setChecked(null);
      setError(humanizeError(e));
    } finally {
      running.current = false;
      if (previewRequest.current === controller) previewRequest.current = null;
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={560}
      title={`Add a document source · ${label}`}
      description={
        connected ? "Choose your account and the content to keep in this destination." : KIND_FORM[form.kind].about
      }
      footer={
        <>
          <CancelButton />
          <Button
            type="secondary"
            htmlType="button"
            loading={busy === "check"}
            disabled={busy !== null || problems.length > 0 || !accountReady}
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
      <SourceRegistrationFields
        form={form}
        set={set}
        label={label}
        account={account}
        busy={busy !== null}
        allowShared={allowShared}
      />
      {problems.length > 0 && sourceFormTouched(form) && (
        <ul className="text-xs text-text-muted list-disc pl-5" aria-label="Before checking">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      )}
      {checked && passed && (
        <ProbeResult
          connected={connected}
          probe={checked.probe}
          lead={`${KIND_FORM[form.kind].reads}: ${normalisedName(form.name)} lists`}
        />
      )}
      {checked && !passed && <p className="text-xs text-text-muted">The form changed since the check: check again.</p>}
      {preview?.fingerprint === fingerprint && <SourceLinkPreview links={preview.links} expectedKind={form.kind} />}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function CheckSourceDialog({
  open,
  onOpenChange,
  org,
  namespace,
  name,
  connected = false,
}: DialogProps & { name: string; connected?: boolean }) {
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
      description="Checks access to this source; nothing is imported."
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
      {result?.reachable && <ProbeResult connected={connected} probe={result} lead="Reachable: it lists" />}
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
      const out = await syncKnowledgeSourceNow({ org, namespace }, name, full);
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
      await rotateKnowledgeSource({ org, namespace }, name, credential.trim());
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
      const out = await removeKnowledgeSource({ org, namespace }, name, typed.trim(), deleteDocuments);
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
