import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { updateKnowledgeMetadata, type Identity, type KnowledgeEntry } from "../api";
import { toast, toastError } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { isCommitted } from "../lib/errors";

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
 * Without `reindex` it doesn't rewrite chunks: each chunk's stored context
 * (embedded and lexically scored) keeps the OLD title + description. The
 * doc row updates immediately; chunks stay as-is until a reindex or a full
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
      toastError("Save failed", e);
      if (isCommitted(e)) {
        onSaved();
        onOpenChange(false);
      }
    } finally {
      setBusy(false);
    }
  };

  const close = (v: boolean) => {
    if (!busy) onOpenChange(v);
  };

  return (
    <Modal open={open} onOpenChange={close} title="Edit details" className="w-full max-w-[540px]">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSave();
        }}
      >
        <div className="space-y-4">
          <TextField
            id="edit-doc-source-uri"
            label="Source URI (read-only — stable identifier the agent cites)"
            value={entry.source_uri}
            onChange={() => undefined}
            readOnly
          />
          <TextField
            id="edit-doc-title"
            label="Title"
            value={title}
            onChange={setTitle}
            placeholder={entry.original_filename || "Onboarding guide"}
          />
          <TextAreaField
            id="edit-doc-description"
            label="Description"
            value={description}
            onChange={setDescription}
            placeholder="What's in this document, in one or two sentences."
            rows={3}
          />
          <TextField
            id="edit-doc-tags"
            label="Tags (comma-separated)"
            value={tagsInput}
            onChange={setTagsInput}
            placeholder="reference, work"
          />
          <div>
            <Checkbox
              label="Re-embed chunks with the new metadata"
              aria-describedby="edit-doc-reindex-hint"
              checked={reindex}
              onCheckedChange={(x) => setReindex(x === true)}
            />
            <p id="edit-doc-reindex-hint" className="mt-1 text-xs text-text-muted">
              Rewrites each chunk with the new title + description for both search legs, without re-uploading. Re-embeds
              every chunk of the document on each save.
            </p>
          </div>
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
            Save
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
