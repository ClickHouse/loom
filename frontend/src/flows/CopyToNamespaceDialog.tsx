import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextField } from "@clickhouse/click-ui/TextField";
import { copyMemoryTo, listMine, type Identity, type MyNamespaceEntry } from "../api";
import { humanizeError } from "../lib/errorWords";
import { isCollectionName } from "../lib/namespace";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { Alert, fieldError } from "../components/ui";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // The memory's current location (org + source namespace).
  source: Identity;
  memoryKey: string;
}

// Roles that can WRITE a namespace (viewers can read but not accept a copy).
const WRITABLE = new Set(["admin", "writer", "jwt"]);

export function CopyToNamespaceDialog({ open, onOpenChange, source, memoryKey }: Props) {
  const [targets, setTargets] = useState<MyNamespaceEntry[]>([]);
  const [loadingTargets, setLoadingTargets] = useState(false);
  const [target, setTarget] = useState("");
  const [newKey, setNewKey] = useState("");
  const [rationale, setRationale] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the namespaces this user can write to (excluding the source).
  useEffect(() => {
    if (!open) return;
    setLoadingTargets(true);
    listMine(source.org)
      .then((rows) =>
        setTargets(
          rows.filter(
            (r) =>
              WRITABLE.has(r.role) &&
              r.status !== "pending" &&
              r.namespace !== source.namespace &&
              // A repository is read-only reference data, never a copy target
              // — even for its maintainers; a collection is shared in the Library.
              r.kind !== "code" &&
              !isCollectionName(r.namespace),
          ),
        ),
      )
      .catch(() => setTargets([]))
      .finally(() => setLoadingTargets(false));
  }, [open, source.org, source.namespace]);

  const reset = () => {
    setTarget("");
    setNewKey("");
    setRationale("");
    setError(null);
    setBusy(false);
  };

  const keyError =
    newKey.trim() === ""
      ? null
      : !/^[a-z][a-z0-9_]{0,79}$/.test(newKey.trim())
        ? "Must be lower_snake_case, start with a letter, ≤80 chars"
        : null;

  const canSubmit = target !== "" && !keyError && !busy;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const res = await copyMemoryTo(source, memoryKey, target, {
        targetKey: newKey.trim() || undefined,
        rationale: rationale.trim() || undefined,
      });
      toast(`Copied to ${res.target_namespace} as ${res.target_key}`, "ok");
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  const close = (v: boolean) => {
    if (!v) reset();
    onOpenChange(v);
  };

  return (
    <Modal open={open} onOpenChange={close} title="Copy to namespace" className="w-full max-w-[520px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
      >
        <div className="space-y-4">
          <p className="text-sm text-text-muted leading-relaxed">
            Copies <span className="font-mono text-text">{memoryKey}</span> from{" "}
            <span className="font-mono text-text">{source.namespace}</span> into another namespace. The original stays
            where it is; the copy is re-embedded under the target's own embedder and keeps a link back to its source.
          </p>
          <Select
            id="copy-target-namespace"
            label="Destination namespace (you can write to)"
            value={target || undefined}
            onSelect={setTarget}
            disabled={loadingTargets}
            placeholder={
              loadingTargets
                ? "Loading namespaces"
                : targets.length === 0
                  ? "No other writable namespaces"
                  : "Select a namespace…"
            }
          >
            {targets.map((t) => (
              <Select.Item key={t.namespace} value={t.namespace}>
                {t.namespace} · {t.role}
              </Select.Item>
            ))}
          </Select>
          <TextField
            id="copy-new-key"
            label="Key in destination (optional — defaults to the same key)"
            value={newKey}
            onChange={setNewKey}
            placeholder={memoryKey}
            {...fieldError("copy-new-key-error", keyError)}
          />
          <TextField
            id="copy-reason"
            label="Reason (optional — recorded on the audit log)"
            value={rationale}
            onChange={setRationale}
            placeholder="promoting a validated finding to the shared space"
          />
          {error && <Alert role="alert" state="danger" text={error} />}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!canSubmit} loading={busy}>
            Copy memory
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
