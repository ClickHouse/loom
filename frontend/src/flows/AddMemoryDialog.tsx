import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Plus, AlertCircle } from "lucide-react";
import { setMemory, type Identity } from "../api";
import { sharedTagVocabulary } from "../lib/sharedReads";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Identity;
  onAdded: () => void;
}

// "Never" leaves ttl_seconds unset → no `expires_at`; lives until deleted.
const EXPIRY_OPTIONS: Array<{ label: string; ttlSeconds: number | null }> = [
  { label: "Never", ttlSeconds: null },
  { label: "1 hour", ttlSeconds: 60 * 60 },
  { label: "1 day", ttlSeconds: 24 * 60 * 60 },
  { label: "1 week", ttlSeconds: 7 * 24 * 60 * 60 },
  { label: "30 days", ttlSeconds: 30 * 24 * 60 * 60 },
  { label: "90 days", ttlSeconds: 90 * 24 * 60 * 60 },
  { label: "1 year", ttlSeconds: 365 * 24 * 60 * 60 },
];

export function AddMemoryDialog({ open, onOpenChange, identity, onAdded }: Props) {
  const [memoryKey, setMemoryKey] = useState("");
  const [value, setValue] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [expiryIdx, setExpiryIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Namespace tags, surfaced as datalist suggestions on the tag input.
  const [existingTags, setExistingTags] = useState<string[]>([]);
  useEffect(() => {
    if (!open) return;
    sharedTagVocabulary(identity)
      .then(setExistingTags)
      .catch(() => setExistingTags([]));
  }, [open, identity.org, identity.namespace]);

  const reset = () => {
    setMemoryKey("");
    setValue("");
    setTagsInput("");
    setExpiryIdx(0);
    setError(null);
    setBusy(false);
  };

  const keyError =
    memoryKey.trim() === ""
      ? null
      : !/^[a-z][a-z0-9_]{0,79}$/.test(memoryKey.trim())
        ? "Must be lower_snake_case, start with a letter, ≤80 chars"
        : null;

  const canSubmit = memoryKey.trim() !== "" && value.trim() !== "" && !keyError && !busy;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t.length > 0);
    try {
      // Attribute to `loom-ui`, not the picker (which is "view as", not the
      // writer) — Conflicts/Audit/Spending key off `agent`.
      await setMemory({ ...identity, agent: "loom-ui" }, memoryKey.trim(), value.trim(), {
        memoryType: "semantic",
        tags,
        ttlSeconds: EXPIRY_OPTIONS[expiryIdx].ttlSeconds,
      });
      toast("Memory added", "ok");
      onAdded();
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
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(560px,92vw)] max-h-[88vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
              <Plus size={16} /> Add memory
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="p-6 space-y-4">
            {/* memory_key */}
            <div>
              <label
                htmlFor="add-memory-key"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Key <span className="text-[var(--color-text-subtle)]">(lower_snake_case)</span>
              </label>
              <input
                id="add-memory-key"
                value={memoryKey}
                onChange={(e) => setMemoryKey(e.target.value)}
                placeholder="user_coffee_preference"
                className={`w-full font-mono text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border outline-none ${
                  keyError
                    ? "border-[var(--color-danger)]"
                    : "border-[var(--color-border)] focus:border-[var(--color-accent)]"
                }`}
                autoFocus
              />
              {keyError && (
                <div className="mt-1 text-xs text-[var(--color-danger)] flex items-center gap-1">
                  <AlertCircle size={12} /> {keyError}
                </div>
              )}
            </div>

            {/* value */}
            <div>
              <label
                htmlFor="add-memory-value"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Value
              </label>
              <textarea
                id="add-memory-value"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder="User takes coffee black, no sugar."
                rows={4}
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none resize-y leading-relaxed"
              />
            </div>

            {/* tags */}
            <div>
              <label
                htmlFor="add-memory-tags"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Tags <span className="text-[var(--color-text-subtle)]">(comma-separated, optional)</span>
              </label>
              <input
                id="add-memory-tags"
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                placeholder="preference, beverage"
                list="loom-existing-tags"
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
              {existingTags.length > 0 && (
                <datalist id="loom-existing-tags">
                  {existingTags.map((t) => (
                    <option key={t} value={t} />
                  ))}
                </datalist>
              )}
            </div>

            {/* expiry */}
            <div>
              <label
                htmlFor="add-memory-expiry"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Expires <span className="text-[var(--color-text-subtle)]">(auto-delete after)</span>
              </label>
              <select
                id="add-memory-expiry"
                value={expiryIdx}
                onChange={(e) => setExpiryIdx(parseInt(e.target.value, 10))}
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              >
                {EXPIRY_OPTIONS.map((opt, i) => (
                  <option key={opt.label} value={i}>
                    {opt.label}
                  </option>
                ))}
              </select>
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
              onClick={onSubmit}
              disabled={!canSubmit}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? "Adding…" : "Add memory"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
