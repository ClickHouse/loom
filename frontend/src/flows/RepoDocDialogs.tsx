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
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { addKnowledge, downloadKnowledge, getKnowledge, textDownloadName, type KnowledgeEntry } from "../api";
import { humanizeError, relativeTime } from "../lib/format";
import { toastError } from "../components/Toast";
import { TabTrigger } from "../components/ui/TabTrigger";
import { Loading } from "../components/ui/Loading";
import { readDocumentFile, slugify, type ReadDocument } from "../lib/docFiles";
import { CancelButton, DialogFrame, ErrorNote } from "./TaskDialogFrame";

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
      title={`Add a document to ${repo}`}
      description="A design note, runbook or spec agents should read before changing this code. Readers of the repository can read it; it stays as private as the repository."
      footer={
        <>
          <CancelButton />
          <Button
            type="primary"
            htmlType="button"
            disabled={!ready}
            loading={busy === "saving"}
            onClick={() => void save()}
          >
            Add document
          </Button>
        </>
      }
    >
      <Tabs value={mode} onValueChange={(v) => setMode(v === "paste" ? "paste" : "file")}>
        <Tabs.TriggersList aria-label="How to add it">
          <TabTrigger value="file">Upload a file</TabTrigger>
          <TabTrigger value="paste">Paste text</TabTrigger>
        </Tabs.TriggersList>
        <Tabs.Content value="file" className="pt-4">
          {/* Not Click UI's FileUpload: its file input has no name, and it cannot show what was read. */}
          {/* biome-ignore lint/a11y/noStaticElementInteractions: a drop target for dragged files; the "Choose a file" button inside is the keyboard path */}
          {/* biome-ignore lint/a11y/noNoninteractiveElementInteractions: as above */}
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={onDrop}
            className={`rounded border border-dashed p-5 text-center text-sm ${over ? "border-accent bg-accent-soft" : "border-border-strong"}`}
          >
            {file ? (
              <div className="flex items-center justify-center gap-2">
                <Icon name="document" size="sm" aria-hidden="true" />
                <span className="font-medium">{file.blob.name}</span>
                <span className="text-text-muted">
                  · {Math.max(1, Math.round(file.blob.size / 1024))} KB · {file.text.length.toLocaleString()} characters
                  read
                </span>
              </div>
            ) : (
              <p className="text-text-muted">
                {busy === "reading" ? "Reading the file…" : "Drop a PDF, Word, Markdown or text file here"}
              </p>
            )}
            <div className="mt-3 flex justify-center">
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="upload"
                disabled={busy !== null}
                onClick={() => inputRef.current?.click()}
              >
                {file ? "Choose another file" : "Choose a file"}
              </Button>
            </div>
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
        </Tabs.Content>
        <Tabs.Content value="paste" className="pt-4">
          <TextAreaField
            id="rdoc-text"
            label="Text (Markdown is fine)"
            className="font-mono"
            rows={8}
            value={pasted}
            onChange={setPasted}
          />
        </Tabs.Content>
      </Tabs>
      <TextField
        id="rdoc-title"
        label="Title"
        value={title}
        placeholder="MergeTree merge scheduling — design note"
        onChange={setTitle}
      />
      <TextField
        id="rdoc-desc"
        label="What agents should use it for (optional, one line)"
        value={description}
        placeholder="Read before touching merge selection or TTL"
        onChange={setDescription}
      />
      {uri && (
        <p className="text-xs text-text-muted break-all">
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
    getKnowledge({ org, namespace }, doc.source_uri)
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
      title={doc.title || doc.source_uri}
      description={[
        doc.source_uri,
        doc.ingested_at ? `added ${relativeTime(doc.ingested_at)}` : "",
        reads ? `opened by agents ${reads.reads} time${reads.reads === 1 ? "" : "s"} in ${reads.days} days` : "",
      ]
        .filter(Boolean)
        .join(" · ")}
      footer={
        <>
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="download"
            loading={downloading}
            onClick={() => void download()}
          >
            {doc.has_blob ? "Download original" : "Download"}
          </Button>
          <CancelButton label="Close" />
        </>
      }
    >
      {state.kind === "loading" && <Loading label="Loading the document" />}
      {state.kind === "error" && <ErrorNote>{state.message}</ErrorNote>}
      {state.kind === "ready" && (
        <>
          {state.description && <p className="text-sm text-text-muted">{state.description}</p>}
          <p className="text-xs text-text-muted">The text agents read, as Loom stores it.</p>
          <pre className="max-h-[55vh] overflow-auto rounded-md border border-border bg-surface-2 p-3 text-xs leading-relaxed whitespace-pre-wrap break-words font-mono">
            {state.content || "(no text)"}
          </pre>
        </>
      )}
    </DialogFrame>
  );
}
