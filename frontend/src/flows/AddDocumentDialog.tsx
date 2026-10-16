import { useEffect, useRef, useState } from "react";
import { Alert } from "@clickhouse/click-ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { addKnowledge, type Identity } from "../api";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { formatBytesShort, humanizeError } from "../lib/format";
import { arrayBufferToBase64, extractDocxText, extractPdfText, guessContentType } from "../lib/docFiles";

const ACCEPT =
  ".pdf,.docx,.md,.txt,.json,.yaml,.yml,.csv,.html,.xml,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/*";

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
 * Human document upload from the dashboard. Calls knowledge.upsert (upsert is the
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

  // Don't let an in-flight submit get aborted by an accidental close: closing cancels the
  // fetch, which reads "fetch failed" though the server already committed the doc.
  const close = (v: boolean) => {
    if (busy && !v) return;
    if (!v) reset();
    onOpenChange(v);
  };
  const holdWhileBusy = (e: Event) => {
    if (busy) e.preventDefault();
  };

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title={replaceMode ? "Edit content (re-upload)" : "Add document"}
      description={replaceMode?.sourceUri}
      className="w-full max-w-[640px]"
      onEscapeKeyDown={holdWhileBusy}
      onPointerDownOutside={holdWhileBusy}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
      >
        <div className="space-y-4">
          <div>
            <input
              aria-label="Choose a file to upload"
              ref={fileRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => onPickFile(e.target.files?.[0] || null)}
            />
            <Button
              type="secondary"
              htmlType="button"
              iconLeft="upload"
              fillWidth
              onClick={() => fileRef.current?.click()}
            >
              {replaceMode ? "Pick the new file" : "Pick a file"}
            </Button>
            <p className="mt-1.5 text-xs text-text-subtle">
              PDF, Word (.docx), Markdown, text, JSON, YAML, CSV, HTML or XML. Text is extracted in your browser before
              upload — review it below before saving. Scanned/image PDFs need OCR (not supported); paste the text
              directly instead.
            </p>
          </div>
          {/* Locked in Replace mode, so the new content overwrites the document rather
              than creating a second one under another URI. */}
          {!replaceMode && (
            <TextField
              id="add-doc-source-uri"
              label="Source URI (stable identifier the agent cites — a path, URL, or slug)"
              value={sourceUri}
              onChange={setSourceUri}
              placeholder="docs/getting-started/quickstart.md"
            />
          )}
          <TextField
            id="add-doc-title"
            label="Title (optional — overrides filename in the Library list)"
            value={title}
            onChange={setTitle}
            placeholder="Onboarding guide"
          />
          <TextAreaField
            id="add-doc-description"
            label="Description (optional — one-line summary; improves retrieval and shows in the Library list)"
            value={description}
            onChange={setDescription}
            placeholder="What's in this document, in one or two sentences."
            rows={2}
          />
          <div>
            <TextAreaField
              id="add-doc-content"
              label={
                originalBlob
                  ? `Content — extracted from ${originalBlob.name} (${formatBytesShort(originalBlob.size)} original)`
                  : "Content"
              }
              value={content}
              onChange={setContent}
              placeholder="Paste the document text here. Loom chunks + embeds on upload."
              rows={10}
            />
            <div className="mt-1 text-xs text-text-subtle tabular">
              {content.length.toLocaleString()} chars
              {originalBlob && " · original file preserved for download"}
            </div>
          </div>
          <TextField
            id="add-doc-tags"
            label="Tags (comma-separated, optional)"
            value={tagsInput}
            onChange={setTagsInput}
            placeholder="reference, work"
          />
          {error && (
            <div role="alert">
              <Alert state="danger" text={error} />
            </div>
          )}
          {busy && (
            <Alert
              state="info"
              text={`Embedding ${Math.max(1, Math.ceil(content.length / 1000))} ≈chunks. Don't close this dialog — closing it cancels the upload mid-way and the toast you'll get says "fetch failed" even when the server already committed half the work.`}
            />
          )}
        </div>
        <ModalFooter>
          <Button type="secondary" htmlType="button" disabled={busy} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="primary" htmlType="submit" disabled={!canSubmit} loading={busy}>
            {replaceMode ? "Save changes" : "Upload"}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
