import { useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { lockMemory, rejectMemory, type Identity, type LockMode } from "../api";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, ChoiceGroup } from "../components/ui";

const LOCK_MODES = [
  { value: "read_only", label: "Read only", hint: "No edit, no new expiry, no delete" },
  { value: "no_delete", label: "No delete", hint: "Edits are allowed; delete, redact, clear and merge are refused" },
] as const;

/** memory.lock: a mode and the reason, which every refused write then names. */
export function LockMemoryDialog({
  row,
  memoryKey,
  onDone,
  onClose,
}: {
  /** The row's own identity (`rowIdentity`). */
  row: Identity;
  memoryKey: string;
  onDone: () => void;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<LockMode>("read_only");
  return (
    <ReasonDialog
      title="Lock memory"
      intro={
        <>
          Lock <span className="font-mono text-text">{memoryKey}</span> against writes. Only a namespace admin can
          unlock it.
        </>
      }
      confirm="Lock"
      onClose={onClose}
      onSubmit={async (reason) => {
        const r = await lockMemory(row, memoryKey, mode, reason);
        if (!r.locked) throw new Error("This memory is no longer live.");
        toast("Locked", "ok");
        onDone();
      }}
    >
      <ChoiceGroup label="Mode" value={mode} onChange={setMode} options={LOCK_MODES} />
    </ReasonDialog>
  );
}

/** memory.reject: the fact is wrong; it leaves search and is not extracted again. */
export function RejectMemoryDialog({
  row,
  memoryKey,
  onDone,
  onClose,
}: {
  row: Identity;
  memoryKey: string;
  onDone: () => void;
  onClose: () => void;
}) {
  return (
    <ReasonDialog
      title="Mark as wrong"
      intro={
        <>
          <span className="font-mono text-text">{memoryKey}</span> leaves search, and extraction will not write it
          again.
        </>
      }
      confirm="Mark as wrong"
      danger
      onClose={onClose}
      onSubmit={async (reason) => {
        await rejectMemory(row, memoryKey, reason);
        toast("Marked as wrong", "ok");
        onDone();
      }}
    />
  );
}

/** memory.redact: one past version's value scrubbed for good, with the reason, which the
 *  redaction keeps. `onSubmit` throws to keep the dialog open with the reason it failed. */
export function RedactVersionDialog({
  memoryKey,
  label,
  onSubmit,
  onClose,
}: {
  memoryKey: string;
  /** The version as the history names it (`v3`). */
  label: string;
  onSubmit: (reason: string) => Promise<void>;
  onClose: () => void;
}) {
  return (
    <ReasonDialog
      title={`Redact ${label}`}
      intro={
        <>
          The value of {label} of <span className="font-mono text-text">{memoryKey}</span> is scrubbed for good; the
          version and its audit row stay.
        </>
      }
      confirm="Redact"
      danger
      onClose={onClose}
      onSubmit={onSubmit}
    />
  );
}

function ReasonDialog({
  title,
  intro,
  confirm,
  danger = false,
  onSubmit,
  onClose,
  children,
}: {
  title: string;
  intro: React.ReactNode;
  confirm: string;
  danger?: boolean;
  onSubmit: (reason: string) => Promise<void>;
  onClose: () => void;
  children?: React.ReactNode;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = reason.trim() !== "" && !busy;

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(reason.trim());
      onClose();
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Modal open onOpenChange={(v) => !v && !busy && onClose()} title={title} className="w-full max-w-[480px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted leading-relaxed">{intro}</p>
          {children}
          <TextField id="memory-action-reason" label="Reason" value={reason} onChange={setReason} />
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button type={danger ? "danger" : "primary"} htmlType="submit" disabled={!ready} loading={busy}>
            {confirm}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
