import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, FolderInput, AlertCircle } from "lucide-react";
import { copyMemoryTo, listMine, type Identity, type MyNamespaceEntry } from "../api";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  // The memory's current location (org + source namespace).
  source: Identity;
  memoryKey: string;
  // Called after a successful copy so the caller can refresh.
  onCopied?: () => void;
}

// Roles that can WRITE a namespace (viewers can read but not accept a copy).
const WRITABLE = new Set(["admin", "writer", "jwt"]);

export function CopyToNamespaceDialog({ open, onOpenChange, source, memoryKey, onCopied }: Props) {
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
              // A code index is read-only reference data, never a copy target
              // — even for its maintainers.
              r.kind !== "code",
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
      onCopied?.();
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(520px,92vw)] max-h-[88vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
              <FolderInput size={16} /> Copy to namespace
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="p-6 space-y-4">
            <p className="text-sm text-[var(--color-text-muted)] leading-relaxed">
              Copies <span className="font-mono text-[var(--color-text)]">{memoryKey}</span> from{" "}
              <span className="font-mono text-[var(--color-text)]">{source.namespace}</span> into another namespace. The
              original stays where it is; the copy is re-embedded under the target's own embedder and keeps a link back
              to its source.
            </p>

            {/* target namespace */}
            <div>
              <label
                htmlFor="copy-target-namespace"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Destination namespace <span className="text-[var(--color-text-subtle)]">(you can write to)</span>
              </label>
              <select
                id="copy-target-namespace"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                disabled={loadingTargets}
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              >
                <option value="">
                  {loadingTargets
                    ? "Loading…"
                    : targets.length === 0
                      ? "No other writable namespaces"
                      : "Select a namespace…"}
                </option>
                {targets.map((t) => (
                  <option key={t.namespace} value={t.namespace}>
                    {t.namespace} · {t.role}
                  </option>
                ))}
              </select>
            </div>

            {/* optional new key */}
            <div>
              <label htmlFor="copy-new-key" className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
                Key in destination{" "}
                <span className="text-[var(--color-text-subtle)]">(optional — defaults to the same key)</span>
              </label>
              <input
                id="copy-new-key"
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder={memoryKey}
                className={`w-full font-mono text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border outline-none ${
                  keyError
                    ? "border-[var(--color-danger)]"
                    : "border-[var(--color-border)] focus:border-[var(--color-accent)]"
                }`}
              />
              {keyError && (
                <div className="mt-1 text-xs text-[var(--color-danger)] flex items-center gap-1">
                  <AlertCircle size={12} /> {keyError}
                </div>
              )}
            </div>

            {/* rationale */}
            <div>
              <label htmlFor="copy-reason" className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
                Reason <span className="text-[var(--color-text-subtle)]">(optional — recorded on the audit log)</span>
              </label>
              <input
                id="copy-reason"
                value={rationale}
                onChange={(e) => setRationale(e.target.value)}
                placeholder="promoting a validated finding to the shared space"
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
            </div>

            {error && (
              <div className="px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-danger)]/30 text-xs text-[var(--color-danger)] flex items-center gap-2">
                <AlertCircle size={13} /> {error}
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-[var(--color-border)] bg-[var(--color-surface)]">
            <Dialog.Close className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md">
              Cancel
            </Dialog.Close>
            <button
              type="button"
              onClick={onSubmit}
              disabled={!canSubmit}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? "Copying…" : "Copy memory"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
