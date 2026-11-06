/**
 * A repository's documents: add one, read one.
 *
 * Add asks only what a person knows — the file (dropped, picked or pasted),
 * a title, and one line on what agents should use it for. The address a
 * document is stored under (`repo-doc://<repo>/<slug>`) is made for them and
 * shown, never typed; a clash with an existing document gets a free suffix
 * instead of silently replacing it. A file's bytes are uploaded and ingested
 * (`knowledge.ingest`): the server's extractor reads it, the browser does not.
 *
 * Read shows the text as Loom stores it (what agents read) and offers the
 * original file. A preview is sent as the dashboard (`consumer: loom-ui`),
 * so a maintainer checking a document never counts as an agent opening it.
 */
import { useEffect, useRef, useState, type DragEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { addKnowledge, downloadKnowledge, getKnowledge, textDownloadName, type KnowledgeEntry } from "../api";
import { humanizeError, relativeTime } from "../lib/format";
import { toast, toastError } from "../components/Toast";
import { TabTrigger } from "../components/ui/TabTrigger";
import { Loading } from "../components/ui/Loading";
import { slugify } from "../lib/docFiles";
import { fileVerdict, type IngestPhase, ingestFile, ingestStep } from "../lib/fileIngest";
import { AllowPartial, IngestProgress, PickedFile, readableFormats, useKnowledgeFormats } from "./FileIngest";
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
  /** `lost`: what a file published as partial lost ([] for nothing named); null when whole. */
  onAdded: (title: string, lost: string[] | null) => void;
}) {
  const [mode, setMode] = useState<"file" | "paste">("file");
  const [file, setFile] = useState<File | null>(null);
  const [pasted, setPasted] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [allowPartial, setAllowPartial] = useState(false);
  // "upload" can be cancelled (nothing is queued yet); "queue" and "saving" hold the dialog
  // open (the call may have taken effect); "follow" closes, the job going on on the server.
  const [busy, setBusy] = useState<IngestPhase | "saving" | null>(null);
  const [step, setStep] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const formats = useKnowledgeFormats({ org, namespace }, open);
  const verdict = file ? fileVerdict(file, formats) : null;
  // Closing (the card unmounts the dialog) stops the upload and the following.
  const following = useRef<AbortController | null>(null);
  useEffect(() => () => following.current?.abort(), []);
  const taken = new Set(takenUris);
  const uri = title.trim() ? repoDocUri(repo, title, taken) : "";
  const ready = !!title.trim() && busy === null && (mode === "file" ? !!file && !verdict?.refusal : !!pasted.trim());

  const take = (f: File | null | undefined) => {
    if (!f) return;
    setError(null);
    setStep("");
    setFile(f);
    if (!title.trim()) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "));
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    if (busy === null) take(e.dataTransfer.files?.[0]); // not while a file is on its way
  };

  const saveFile = async (f: File): Promise<string[] | null> => {
    const ctrl = new AbortController();
    following.current = ctrl;
    setBusy("upload");
    // expected_revision 0: an address taken by a document past the list's first page
    // fails `revision_conflict` instead of becoming that document's next revision.
    const status = await ingestFile(
      { org, namespace },
      f,
      {
        source_uri: uri,
        title: title.trim(),
        description: description.trim(),
        allow_partial: allowPartial,
        expected_revision: 0,
      },
      (line, phase) => {
        setStep(line);
        setBusy(phase);
      },
      { signal: ctrl.signal },
    );
    if (status.state === "ready") return null;
    if (status.state === "partial") return status.coverage?.lost ?? [];
    setStep("");
    throw new Error(status.found ? `${f.name}: ${ingestStep(status)}` : "The ingest job was not found.");
  };

  const save = async () => {
    if (!ready) return;
    setBusy("saving");
    setError(null);
    try {
      let lost: string[] | null = null;
      if (mode === "file" && file) lost = await saveFile(file);
      else
        await addKnowledge(
          { org, namespace },
          { source_uri: uri, content: pasted, title: title.trim(), description: description.trim(), upsert: false },
        );
      onAdded(title.trim(), lost);
      onOpenChange(false);
    } catch (e) {
      if (following.current?.signal.aborted) return; // closed: the job goes on on the server
      setError(humanizeError(e)); // a job's reason passes through; a dropped connection is worded
    } finally {
      setBusy(null);
    }
  };

  // Closing mid-upload would cancel the bytes or the ingest call: held. Once the job is
  // queued it goes on without the dialog, and the card says so.
  const close = (v: boolean) => {
    if (!v && (busy === "queue" || busy === "saving")) return;
    if (!v && busy === "follow")
      toast(`${title.trim()} goes on extracting; it is listed once it publishes and the list is reloaded`, "info");
    onOpenChange(v);
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={close}
      width={600}
      title={`Add a document to ${repo}`}
      description="A design note, runbook or spec agents should read before changing this code. Readers of the repository can read it; it stays as private as the repository."
      footer={
        <>
          <CancelButton
            label={busy === "follow" ? "Close" : "Cancel"}
            disabled={busy === "queue" || busy === "saving"}
          />
          <Button
            type="primary"
            htmlType="button"
            disabled={!ready}
            loading={busy !== null}
            onClick={() => void save()}
          >
            Add document
          </Button>
        </>
      }
    >
      <Tabs value={mode} onValueChange={(v) => busy === null && setMode(v === "paste" ? "paste" : "file")}>
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
            {file && verdict ? (
              <PickedFile file={file} verdict={verdict} />
            ) : (
              <p className="text-text-muted">
                Drop a file here{readableFormats(formats) ? ` (${readableFormats(formats)})` : ""}
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
              onChange={(e) => take(e.target.files?.[0])}
              tabIndex={-1}
            />
          </div>
          <div className="mt-3 space-y-2">
            {verdict?.refusal && <ErrorNote>{verdict.refusal}</ErrorNote>}
            <AllowPartial checked={allowPartial} onChange={setAllowPartial} disabled={busy !== null} />
          </div>
        </Tabs.Content>
        <Tabs.Content value="paste" className="pt-4">
          <TextAreaField
            id="rdoc-text"
            disabled={busy !== null}
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
        disabled={busy !== null}
        value={title}
        placeholder="MergeTree merge scheduling — design note"
        onChange={setTitle}
      />
      <TextField
        id="rdoc-desc"
        disabled={busy !== null}
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
      <IngestProgress step={step} />
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
