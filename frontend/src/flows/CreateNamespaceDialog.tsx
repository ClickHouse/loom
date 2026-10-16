import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Plus, Lock, Globe, AlertCircle } from "lucide-react";
import { registerNamespace, setSavedNamespace } from "../api";
import { useStore } from "../lib/state";
import { toast } from "../components/Toast";
import { humanizeError } from "../lib/format";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated?: (namespace: string) => void;
}

/**
 * Create-namespace dialog wired into the namespace picker.
 *
 * Anyone authenticated can create a namespace (`namespace.register` is
 * VIEWER-gated and the caller auto-becomes admin). Visibility defaults
 * to private — public is the deliberate choice for team scratchpads.
 *
 * After creation the SPA switches identity to the new namespace so the
 * user lands in their fresh workspace.
 */
export function CreateNamespaceDialog({ open, onOpenChange, onCreated }: Props) {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const setIdentity = useStore((s) => s.setIdentity);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  // Only meaningful when visibility=public. When true, viewer self-
  // joins ALSO go pending (writer/admin already always do). Used for
  // sensitive public namespaces where even read-access needs admin
  // sign-off.
  const [requiresApproval, setRequiresApproval] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setDescription("");
    setVisibility("private");
    setRequiresApproval(false);
    setBusy(false);
    setError(null);
  };

  // Namespaces are lowercase + kebab-case by convention; reject obviously
  // malformed input before round-tripping to the server.
  const trimmed = name.trim();
  const nameValid = /^[a-z0-9][a-z0-9-]{0,127}$/.test(trimmed);
  const canSubmit = nameValid && !busy;

  const onSubmit = async () => {
    if (!canSubmit || !identity) return;
    setBusy(true);
    setError(null);
    try {
      await registerNamespace(
        { org: identity.org, namespace: trimmed },
        {
          owner_email: whoami?.subject || "",
          description: description.trim(),
          visibility,
          requires_approval: visibility === "public" ? requiresApproval : false,
        },
      );
      toast(`Created ${trimmed}`, "ok");
      // Switch to the freshly-created namespace.
      setIdentity({ ...identity, namespace: trimmed });
      setSavedNamespace(trimmed);
      onCreated?.(trimmed);
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
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(520px,92vw)] rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
              <Plus size={16} /> Create namespace
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              aria-label="Close"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="p-6 space-y-4">
            <div>
              <label
                htmlFor="create-ns-name"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Name <span className="text-[var(--color-text-subtle)]">(lowercase, kebab-case)</span>
              </label>
              <input
                id="create-ns-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-team"
                autoFocus
                className="w-full px-3 py-2 text-base font-mono rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
              />
              {trimmed && !nameValid && (
                <div className="mt-1 text-2xs text-warn">
                  Use lowercase letters, digits, and dashes only. Start with a letter or digit.
                </div>
              )}
            </div>

            <div>
              <label
                htmlFor="create-ns-description"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Description <span className="text-[var(--color-text-subtle)]">(optional)</span>
              </label>
              <input
                id="create-ns-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="e.g. ClickHouse kernel team shared memory"
                className="w-full px-3 py-2 text-base rounded-md border border-[var(--color-border)] bg-[var(--color-surface-2)] outline-none focus:border-[var(--color-accent)]"
              />
            </div>

            <div>
              <div className="block text-xs font-medium text-[var(--color-text-muted)] mb-2">Visibility</div>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => setVisibility("private")}
                  className={`px-3 py-2.5 rounded-md border text-left transition-colors ${
                    visibility === "private"
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
                      : "border-[var(--color-border)] bg-[var(--color-surface-2)] hover:bg-[var(--color-surface-3)]"
                  }`}
                >
                  <div className="flex items-center gap-1.5 text-sm font-medium">
                    <Lock size={12} /> Private
                  </div>
                  <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                    Only you and people you grant. Default.
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => setVisibility("public")}
                  className={`px-3 py-2.5 rounded-md border text-left transition-colors ${
                    visibility === "public"
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
                      : "border-[var(--color-border)] bg-[var(--color-surface-2)] hover:bg-[var(--color-surface-3)]"
                  }`}
                >
                  <div className="flex items-center gap-1.5 text-sm font-medium">
                    <Globe size={12} /> Public
                  </div>
                  <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                    Anyone in the org can browse + join. For team scratchpads.
                  </div>
                </button>
              </div>
              {visibility === "public" && (
                <label className="mt-3 flex items-start gap-2.5 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={requiresApproval}
                    onChange={(e) => setRequiresApproval(e.target.checked)}
                    className="mt-0.5"
                  />
                  <div>
                    <div className="text-xs">Require approval for read-only (viewer) joins too</div>
                    <div className="text-2xs text-[var(--color-text-muted)]">
                      Writer and admin joins always need approval. Turn this on if even viewers should be reviewed
                      first.
                    </div>
                  </div>
                </label>
              )}
            </div>

            {error && (
              <div className="flex gap-2 px-3 py-2 text-xs text-danger bg-danger/15 rounded-md">
                <AlertCircle size={14} className="shrink-0 mt-0.5" />
                <span>{error}</span>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 px-6 py-3.5 border-t border-[var(--color-border)] bg-[var(--color-surface-2)] rounded-b-2xl">
            <Dialog.Close className="px-3 py-1.5 text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)]">
              Cancel
            </Dialog.Close>
            <button
              type="button"
              onClick={onSubmit}
              disabled={!canSubmit}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-50"
            >
              <Plus size={12} /> {busy ? "Creating…" : "Create"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
