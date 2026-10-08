import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X, Upload, AlertCircle, FileText } from "lucide-react";
import { addKnowledge, type Identity } from "../api";
import { toast } from "../components/Toast";
import { formatBytesShort, humanizeError } from "../lib/format";
import { arrayBufferToBase64, extractDocxText, extractPdfText, guessContentType } from "../lib/docFiles";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Pick<Identity, "org" | "namespace">;
  onAdded: () => void;
  /** When set, the dialog opens in Replace mode: the source_uri is
   *  locked to this value (the new upload replaces the existing
   *  document) and the title/tags are pre-filled from the prior
   *  document so the user can adjust without retyping everything.
   *  Omit for the standard add-new flow. */
  replaceMode?: {
    sourceUri: string;
    title?: string;
    description?: string;
    tags?: string[];
  };
}

/**
 * Human document upload for /me. Calls knowledge.upsert (upsert is the
 * usual want — re-pasting the same source replaces chunks).
 *
 * Two file paths converge into the same uploaded payload:
 *   - Plain-text (.md/.txt/.json/.csv/.html/.xml/.yaml) — read directly
 *     and posted as `content`.
 *   - Binary (.pdf, .docx) — text extracted in-browser via lazy-
 *     loaded pdfjs / mammoth and posted as `content`, AND the original
 *     bytes are sent as `original_bytes_b64` so the server preserves
 *     them for later download.
 *
 * Open in `replaceMode` to lock the source_uri and pre-fill title +
 * tags from an existing document — used by the Library row's
 * Replace button to re-upload-as-new-version.
 */
export function AddDocumentDialog({ open, onOpenChange, identity, onAdded, replaceMode }: Props) {
  const [sourceUri, setSourceUri] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // When opening in Replace mode, hydrate the form with the existing
  // document's metadata so the user only has to pick the new file.
  useEffect(() => {
    if (!open) return;
    if (replaceMode) {
      setSourceUri(replaceMode.sourceUri);
      setTitle(replaceMode.title || "");
      setDescription(replaceMode.description || "");
      setTagsInput((replaceMode.tags || []).join(", "));
    }
  }, [open, replaceMode]);
  // Original-file capture for the download/replace flow. Populated when
  // the user picks a PDF/DOCX/etc; null when the user pasted plain
  // text directly (nothing to preserve in that case).
  const [originalBlob, setOriginalBlob] = useState<{ b64: string; name: string; type: string; size: number } | null>(
    null,
  );

  const reset = () => {
    setSourceUri("");
    setTitle("");
    setDescription("");
    setContent("");
    setTagsInput("");
    setError(null);
    setBusy(false);
    setOriginalBlob(null);
  };

  const onPickFile = async (file: File | null) => {
    if (!file) return;
    setError(null);
    // Cap upload size — knowledge.upsert ships the original bytes as
    // base64 in a JSON body. 25 MB raw ≈ 33 MB base64, which is
    // already heavy for a JSON request. Anything bigger should land
    // via the SDK / pipeline, not the dashboard.
    const MAX_BYTES = 25 * 1024 * 1024;
    if (file.size > MAX_BYTES) {
      setError(
        `File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Library uploads cap at 25 MB — split the document or paste the text directly.`,
      );
      return;
    }
    const ext = file.name.toLowerCase().split(".").pop() || "";
    const isText =
      file.type.startsWith("text/") ||
      ["md", "txt", "json", "yaml", "yml", "csv", "html", "xml", "markdown", "log"].includes(ext);
    try {
      let text: string;
      if (ext === "pdf" || file.type === "application/pdf") {
        setBusy(true);
        text = await extractPdfText(file);
      } else if (
        ext === "docx" ||
        file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      ) {
        setBusy(true);
        text = await extractDocxText(file);
      } else if (isText) {
        text = await file.text();
      } else {
        setError(
          `Unsupported file type (${file.type || ext || "unknown"}). Supported: .pdf, .docx, .md, .txt, .json, .yaml, .csv, .html, .xml. For other formats, paste the text directly.`,
        );
        return;
      }
      if (!text.trim()) {
        setError(
          `Could not extract any text from ${file.name}. Scanned/image PDFs need OCR — paste the text directly instead.`,
        );
        return;
      }
      setContent(text);
      if (!sourceUri) setSourceUri(file.name);
      if (!title) setTitle(file.name.replace(/\.[^.]+$/, ""));
      // Capture the original bytes so the user can later download the
      // file as they uploaded it (PDF/DOCX preserved end-to-end).
      const buf = await file.arrayBuffer();
      setOriginalBlob({
        b64: arrayBufferToBase64(buf),
        name: file.name,
        type: file.type || guessContentType(ext),
        size: buf.byteLength,
      });
    } catch (e) {
      setError(`Could not read file: ${humanizeError(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = sourceUri.trim() !== "" && content.trim() !== "" && !busy;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const tags = tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    try {
      const r = await addKnowledge(identity, {
        source_uri: sourceUri.trim(),
        title: title.trim(),
        description: description.trim(),
        content,
        tags,
        original_bytes_b64: originalBlob?.b64,
        original_filename: originalBlob?.name,
        blob_content_type: originalBlob?.type,
      });
      const msg = replaceMode ? `Replaced ${replaceMode.sourceUri}` : "Document uploaded";
      toast(msg, "ok");
      // Heads-up when this upload duplicates an existing doc under a
      // different source_uri. We don't block (the user may want both
      // for retrieval reasons) but we surface so they can dedupe if
      // intended.
      if (r.duplicate_of_uri && r.duplicate_of_uri !== sourceUri.trim()) {
        toast(`Same content already in your library as "${r.duplicate_of_uri}" — keeping both`, "info");
      }
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
        // Don't let an in-flight submit get aborted by an accidental
        // close. Closing the dialog cancels the fetch on the client
        // side, surfacing as "fetch failed" even though the server's
        // already committed the doc — confusing.
        if (busy && !v) return;
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(640px,92vw)] max-h-[90vh] overflow-y-auto rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!"
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
          }}
          onPointerDownOutside={(e) => {
            if (busy) e.preventDefault();
          }}
          onInteractOutside={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--color-border)]">
            <Dialog.Title className="text-lg font-semibold tracking-tight flex items-center gap-2">
              <FileText size={16} /> {replaceMode ? "Edit content (re-upload)" : "Add document"}
              {replaceMode && (
                <span className="font-mono text-xs text-[var(--color-text-muted)] font-normal truncate max-w-[280px]">
                  {replaceMode.sourceUri}
                </span>
              )}
            </Dialog.Title>
            <Dialog.Close
              className="p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-30 disabled:cursor-not-allowed"
              aria-label="Close"
              disabled={busy}
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="p-6 space-y-4">
            {/* file picker (optional shortcut) */}
            <div>
              <input
                aria-label="Choose a file to upload"
                ref={fileRef}
                type="file"
                accept=".pdf,.docx,.md,.txt,.json,.yaml,.yml,.csv,.html,.xml,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/*"
                className="hidden"
                onChange={(e) => onPickFile(e.target.files?.[0] || null)}
              />
              <button
                onClick={() => fileRef.current?.click()}
                className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg border border-dashed border-[var(--color-border-strong)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] w-full justify-center"
              >
                <Upload size={14} /> {replaceMode ? "Pick the new file" : "Pick a file"} (.pdf, .docx, .md, .txt, .json,
                .csv, …)
              </button>
              <div className="mt-1.5 text-2xs text-[var(--color-text-subtle)] italic">
                Text is extracted in your browser before upload — review it below before saving. Scanned/image PDFs need
                OCR (not supported); paste the text directly instead.
              </div>
            </div>

            {/* source_uri — locked in Replace mode so the new content
                overwrites the existing document instead of creating a
                new one with a different URI. */}
            {!replaceMode && (
              <div>
                <label
                  htmlFor="add-doc-source-uri"
                  className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
                >
                  Source URI{" "}
                  <span className="text-[var(--color-text-subtle)]">
                    (stable identifier the agent cites — a path, URL, or slug)
                  </span>
                </label>
                <input
                  id="add-doc-source-uri"
                  value={sourceUri}
                  onChange={(e) => setSourceUri(e.target.value)}
                  placeholder="docs/getting-started/quickstart.md"
                  className="w-full font-mono text-sm px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
                />
              </div>
            )}

            {/* title */}
            <div>
              <label
                htmlFor="add-doc-title"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Title{" "}
                <span className="text-[var(--color-text-subtle)]">
                  (optional — overrides filename in the Library list)
                </span>
              </label>
              <input
                id="add-doc-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Onboarding guide"
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
            </div>

            {/* description */}
            <div>
              <label
                htmlFor="add-doc-description"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Description{" "}
                <span className="text-[var(--color-text-subtle)]">
                  (optional — one-line summary; improves retrieval and shows in the Library list)
                </span>
              </label>
              <textarea
                id="add-doc-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="What's in this document, in one or two sentences."
                rows={2}
                className="w-full text-sm px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none resize-y leading-relaxed"
              />
            </div>

            {/* content */}
            <div>
              <label
                htmlFor="add-doc-content"
                className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5"
              >
                Content{" "}
                {originalBlob && (
                  <span className="text-[var(--color-text-subtle)] font-normal">
                    — extracted from <span className="font-mono">{originalBlob.name}</span> (
                    {formatBytesShort(originalBlob.size)} original)
                  </span>
                )}
              </label>
              <textarea
                id="add-doc-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="Paste the document text here. Loom chunks + embeds on upload."
                rows={10}
                className="w-full text-sm font-mono px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none resize-y leading-relaxed"
              />
              <div className="mt-1 text-2xs text-[var(--color-text-subtle)] tabular">
                {content.length.toLocaleString()} chars
                {originalBlob && " · original file preserved for download"}
              </div>
            </div>

            {/* tags */}
            <div>
              <label htmlFor="add-doc-tags" className="block text-xs font-medium text-[var(--color-text-muted)] mb-1.5">
                Tags <span className="text-[var(--color-text-subtle)]">(comma-separated, optional)</span>
              </label>
              <input
                id="add-doc-tags"
                value={tagsInput}
                onChange={(e) => setTagsInput(e.target.value)}
                placeholder="reference, work"
                className="w-full text-base px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] focus:border-[var(--color-accent)] outline-none"
              />
            </div>

            {error && (
              <div className="px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-danger)]/30 text-xs text-[var(--color-danger)] flex items-center gap-2">
                <AlertCircle size={13} /> {error}
              </div>
            )}

            {busy && (
              <div className="px-3 py-2 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)] text-xs text-[var(--color-text-muted)] leading-relaxed">
                Embedding {Math.max(1, Math.ceil(content.length / 1000))} ≈chunks. Don't close this dialog — closing it
                cancels the upload mid-way and the toast you'll get says "fetch failed" even when the server already
                committed half the work.
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 px-6 py-3 border-t border-[var(--color-border)] bg-[var(--color-surface)]">
            <Dialog.Close
              className="px-3 py-1.5 text-sm font-medium text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] rounded-md disabled:opacity-30 disabled:cursor-not-allowed"
              disabled={busy}
            >
              Cancel
            </Dialog.Close>
            <button
              onClick={onSubmit}
              disabled={!canSubmit}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? (replaceMode ? "Saving…" : "Uploading…") : replaceMode ? "Save changes" : "Upload"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
