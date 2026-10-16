/**
 * A repository's documents: add one, read one.
 *
 * Add asks only what a person knows — the file (dropped, picked or pasted),
 * a title, and one line on what agents should use it for. The address a
 * document is stored under (`repo-doc://<repo>/<slug>`) is made for them and
 * shown, never typed; a clash with an existing document gets a free suffix
 * instead of silently replacing it.
 *
 * Read shows the text as Loom stores it (what agents read) and offers the
 * original file. A preview is sent as the dashboard (`consumer: loom-ui`),
 * so a maintainer checking a document never counts as an agent opening it.
 */
import { useEffect, useRef, useState, type DragEvent } from "react";
import { Download, FileText, Upload } from "lucide-react";
import { addKnowledge, downloadKnowledge, getKnowledge, textDownloadName, type KnowledgeEntry } from "../api";
import { humanizeError, relativeTime } from "../lib/format";
import { toastError } from "../components/Toast";
import { readDocumentFile, slugify, type ReadDocument } from "../lib/docFiles";
import {
  CancelButton,
  DialogFrame,
  ErrorNote,
  Field,
  INPUT_CLS,
  PRIMARY_BTN,
  SECONDARY_BTN,
  TEXTAREA_CLS,
} from "./TaskDialogFrame";

const UI_CONSUMER = "loom-ui";

/** `repo-doc://Org/repo/<slug>`, suffixed `-2`, `-3`… past the URIs taken. */
export function repoDocUri(repo: string, title: string, taken: ReadonlySet<string>): string {
  const base = `repo-doc://${repo}/${slugify(title)}`;
  if (!taken.has(base)) return base;
  for (let i = 2; i < 1000; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
  return `${base}-${Date.now()}`;
}

export function RepoDocAddDialog({
  open,
  onOpenChange,
  org,
  namespace,
  repo,
  takenUris,
  onAdded,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  /** The repository's docs namespace. */
  namespace: string;
  repo: string;
  takenUris: string[];
  onAdded: (title: string) => void;
}) {
  const [mode, setMode] = useState<"file" | "paste">("file");
  const [file, setFile] = useState<ReadDocument | null>(null);
  const [pasted, setPasted] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState<"reading" | "saving" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const taken = new Set(takenUris);
  const uri = title.trim() ? repoDocUri(repo, title, taken) : "";
  const text = mode === "file" ? (file?.text ?? "") : pasted;
  const ready = !!title.trim() && !!text.trim() && busy === null;

  const take = async (f: File | null | undefined) => {
    if (!f) return;
    setBusy("reading");
    setError(null);
    try {
      const doc = await readDocumentFile(f);
      setFile(doc);
      if (!title.trim()) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "));
    } catch (e) {
      setFile(null);
      setError(e instanceof Error ? e.message : humanizeError(e));
    } finally {
      setBusy(null);
    }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    void take(e.dataTransfer.files?.[0]);
  };

  const save = async () => {
    if (!ready) return;
    setBusy("saving");
    setError(null);
    try {
      await addKnowledge(
        { org, namespace },
        {
          source_uri: uri,
          content: text,
          title: title.trim(),
          description: description.trim(),
          upsert: false,
          ...(mode === "file" && file
            ? {
                original_bytes_b64: file.blob.b64,
                original_filename: file.blob.name,
                blob_content_type: file.blob.type,
              }
            : {}),
        },
      );
      onAdded(title.trim());
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={600}
      icon={<FileText size={16} aria-hidden="true" />}
      title={`Add a document to ${repo}`}
      description="A design note, runbook or spec agents should read before changing this code. Readers of the repository can read it; it stays as private as the repository."
      footer={
        <>
          <CancelButton />
          <button type="button" className={PRIMARY_BTN} disabled={!ready} onClick={() => void save()}>
            {busy === "saving" ? "Adding…" : "Add document"}
          </button>
        </>
      }
    >
      <div
        role="tablist"
        aria-label="How to add it"
        className="inline-flex rounded-md border border-border p-0.5 text-xs"
      >
        {(["file", "paste"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            onClick={() => setMode(m)}
            className={`px-3 min-h-8 rounded ${mode === m ? "bg-surface-3 text-text font-medium" : "text-text-muted"}`}
          >
            {m === "file" ? "Upload a file" : "Paste text"}
          </button>
        ))}
      </div>
      {mode === "file" ? (
        // biome-ignore lint/a11y/noStaticElementInteractions: a drop target for dragged files; the "Choose a file" button inside is the keyboard path
        // biome-ignore lint/a11y/noNoninteractiveElementInteractions: as above
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={onDrop}
          className={`rounded-lg border-2 border-dashed p-5 text-center text-sm ${over ? "border-accent bg-accent-soft" : "border-border"}`}
        >
          {file ? (
            <p className="flex items-center justify-center gap-2">
              <FileText size={14} aria-hidden="true" /> <span className="font-medium">{file.blob.name}</span>
              <span className="text-text-muted">
                · {Math.max(1, Math.round(file.blob.size / 1024))} KB · {file.text.length.toLocaleString()} characters
                read
              </span>
            </p>
          ) : (
            <p className="text-text-muted">
              {busy === "reading" ? "Reading the file…" : "Drop a PDF, Word, Markdown or text file here"}
            </p>
          )}
          <button
            type="button"
            className={`${SECONDARY_BTN} mt-3`}
            disabled={busy !== null}
            onClick={() => inputRef.current?.click()}
          >
            <Upload size={13} aria-hidden="true" /> {file ? "Choose another file" : "Choose a file"}
          </button>
          <input
            ref={inputRef}
            type="file"
            className="sr-only"
            aria-label="Choose a file"
            accept=".pdf,.docx,.md,.markdown,.txt,.rst,.adoc,.json,.yaml,.yml,.csv,.html,.xml,text/*"
            onChange={(e) => void take(e.target.files?.[0])}
            tabIndex={-1}
          />
        </div>
      ) : (
        <Field id="rdoc-text" label="Text" hint="Markdown is fine">
          <textarea
            id="rdoc-text"
            className={`${TEXTAREA_CLS} font-mono text-sm`}
            rows={8}
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
          />
        </Field>
      )}
      <Field id="rdoc-title" label="Title">
        <input
          id="rdoc-title"
          className={INPUT_CLS}
          value={title}
          placeholder="MergeTree merge scheduling — design note"
          onChange={(e) => setTitle(e.target.value)}
        />
      </Field>
      <Field id="rdoc-desc" label="What agents should use it for" hint="optional, one line">
        <input
          id="rdoc-desc"
          className={INPUT_CLS}
          value={description}
          placeholder="Read before touching merge selection or TTL"
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>
      {uri && (
        <p className="text-2xs text-text-subtle break-all">
          Saved as <span className="font-mono">{uri}</span>
        </p>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}

export function RepoDocViewer({
  open,
  onOpenChange,
  org,
  namespace,
  doc,
  reads,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  org: string;
  namespace: string;
  doc: KnowledgeEntry;
  /** Agent opens in the window, when known. */
  reads?: { reads: number; days: number } | null;
}) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ready"; content: string; description: string } | { kind: "error"; message: string }
  >({ kind: "loading" });
  const [downloading, setDownloading] = useState(false);
  useEffect(() => {
    let live = true;
    getKnowledge({ org, namespace }, doc.source_uri, { consumer: UI_CONSUMER })
      .then((d) => {
        if (live)
          setState(
            d.found
              ? { kind: "ready", content: d.content || "", description: doc.description || "" }
              : { kind: "error", message: "This document is no longer there." },
          );
      })
      .catch((e) => {
        if (live) setState({ kind: "error", message: humanizeError(e) });
      });
    return () => {
      live = false;
    };
  }, [org, namespace, doc.source_uri, doc.description]);
  const download = async () => {
    setDownloading(true);
    try {
      await downloadKnowledge(
        { org, namespace },
        doc.source_uri,
        doc.original_filename || textDownloadName(doc.source_uri),
      );
    } catch (e) {
      toastError("Download failed", e);
    } finally {
      setDownloading(false);
    }
  };
  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      width={760}
      icon={<FileText size={16} aria-hidden="true" />}
      title={doc.title || doc.source_uri}
      description={
        <>
          <span className="font-mono">{doc.source_uri}</span>
          {doc.ingested_at ? ` · added ${relativeTime(doc.ingested_at)}` : ""}
          {reads ? ` · opened by agents ${reads.reads} time${reads.reads === 1 ? "" : "s"} in ${reads.days} days` : ""}
        </>
      }
      footer={
        <>
          <button type="button" className={SECONDARY_BTN} disabled={downloading} onClick={() => void download()}>
            <Download size={13} aria-hidden="true" />{" "}
            {downloading ? "Downloading…" : doc.has_blob ? "Download original" : "Download"}
          </button>
          <CancelButton label="Close" />
        </>
      }
    >
      {state.kind === "loading" && (
        <p className="text-sm text-text-muted" role="status">
          Opening…
        </p>
      )}
      {state.kind === "error" && <ErrorNote>{state.message}</ErrorNote>}
      {state.kind === "ready" && (
        <>
          {state.description && <p className="text-sm text-text-muted">{state.description}</p>}
          <p className="text-2xs text-text-subtle">The text agents read, as Loom stores it.</p>
          <pre className="max-h-[55vh] overflow-auto rounded-md border border-border bg-surface-2 p-3 text-xs leading-relaxed whitespace-pre-wrap break-words font-mono">
            {state.content || "(no text)"}
          </pre>
        </>
      )}
    </DialogFrame>
  );
}
