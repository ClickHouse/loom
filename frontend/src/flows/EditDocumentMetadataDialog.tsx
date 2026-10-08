import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Pencil, X } from "lucide-react";
import { updateKnowledgeMetadata, type Identity, type KnowledgeEntry } from "../api";
import { toast } from "../components/Toast";
import { humanizeError } from "../lib/format";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Pick<Identity, "org" | "namespace">;
  entry: KnowledgeEntry | null;
  onSaved: () => void;
}

/**
 * Edit title + description + tags without re-uploading the file.
 *
 * Source URI is read-only by design — it's the stable identifier
 * the agent cites, and renaming it would silently break every
 * external reference. To use a different URI, delete + re-add.
 *
 * Doesn't re-chunk or re-embed: the chunk vectors were computed
 * with the OLD title + description prepended. The doc row updates
 * immediately; embeddings stay as-is until the user does a full
 * content re-upload. Note surfaced inline so the user understands.
 */
export function EditDocumentMetadataDialog({ open, onOpenChange, identity, entry, onSaved }: Props) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [reindex, setReindex] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open && entry) {
      setTitle(entry.title || "");
      setDescription(entry.description || "");
      setTagsInput((entry.tags || []).join(", "));
      setReindex(false);
    }
  }, [open, entry]);

  if (!entry) return null;

  const onSave = async () => {
    setBusy(true);
    try {
      const tags = tagsInput
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const r = await updateKnowledgeMetadata(identity, entry.source_uri, {
        title: title.trim(),
        description: description.trim(),
        tags,
        reindex,
      });
      if (reindex && r.chunks_reindexed > 0) {
        toast(`Saved · re-embedded ${r.chunks_reindexed} chunks`, "ok");
      } else {
        toast("Saved", "ok");
      }
      onSaved();
      onOpenChange(false);
    } catch (e) {
      toast(`Save failed: ${humanizeError(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(v) => {
        if (!busy) onOpenChange(v);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(540px,92vw)] max-h-[90vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
              <Pencil size={14} /> Edit details
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-30"
              aria-label="Close"
              disabled={busy}
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="p-6 space-y-4">
            {/* URI — read-only, with a note */}
            <div>
              <label
                htmlFor="edit-doc-source-uri"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Source URI{" "}
                <span className="text-[var(--color-text-subtle)]">(read-only — stable identifier the agent cites)</span>
              </label>
              <input
                id="edit-doc-source-uri"
                value={entry.source_uri}
                readOnly
                className="w-full font-mono text-xs px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] text-[var(--color-text-muted)] cursor-not-allowed"
              />
            </div>

            <div>
              <label
                htmlFor="edit-doc-title"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Title
              </label>
              <input
                id="edit-doc-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={entry.original_filename || "Onboarding guide"}
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
            </div>

            <div>
              <label
                htmlFor="edit-doc-description"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Description
              </label>
              <textarea
                id="edit-doc-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What's in this document, in one or two sentences."
                rows={3}
                className="w-full text-sm px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none resize-y leading-relaxed"
              />
            </div>

            <div>
              <label
                htmlFor="edit-doc-tags"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Tags <span className="text-[var(--color-text-subtle)]">(comma-separated)</span>
              </label>
              <input
                id="edit-doc-tags"
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                placeholder="reference, work"
                className="w-full text-sm px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
            </div>

            <label className="flex items-start gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-2)] px-3 py-2 cursor-pointer hover:border-[var(--color-border-strong)]">
              <input
                type="checkbox"
                checked={reindex}
                onChange={(e) => setReindex(e.target.checked)}
                className="mt-0.5 accent-[var(--color-accent)]"
              />
              <span className="text-xs leading-snug">
                <span className="font-medium text-[var(--color-text)]">Re-embed chunks with the new metadata</span>
                <span className="block text-2xs text-[var(--color-text-muted)] mt-0.5">
                  Picks up the retrieval-quality boost from the new title + description without re-uploading. Costs one
                  OpenAI batch call per save.
                </span>
              </span>
            </label>
          </div>

          <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-[var(--color-border)] bg-[var(--color-surface)]">
            <Dialog.Close
              disabled={busy}
              className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md disabled:opacity-30"
            >
              Cancel
            </Dialog.Close>
            <button
              onClick={onSave}
              disabled={busy}
              className="px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-40"
            >
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
