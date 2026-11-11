import { useEffect, useRef, useState } from "react";
import { Alert } from "../components/ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { addKnowledge, cancelIngestion, type Identity, type IngestCancelResult } from "../api";
import { toast, toastError } from "../components/Toast";
import { DropZone } from "../components/ui/DropZone";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { TabTrigger } from "../components/ui/TabTrigger";
import { foldersNotRead, type PickedEntry } from "../lib/batchIngest";
import { humanizeError } from "../lib/errorWords";
import {
  FollowLost,
  fileVerdict,
  type IngestPhase,
  ingestFile,
  ingestStep,
  tagsFrom,
  titleFromName,
} from "../lib/fileIngest";
import { BatchUpload } from "./BatchUpload";
import { AllowPartial, IngestProgress, PickedFile, readableFormats, useKnowledgeFormatsRead } from "./FileIngest";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Pick<Identity, "org" | "namespace">;
  /** Whether a batch has started, for the page to keep the dialog open until Done. */
  onBatchStarted?: (started: boolean) => void;
  /** What the place the files go to is called on screen; its namespace when unset. */
  place?: string;
  onAdded: () => void;
  /** When set, the dialog opens in Replace mode: the source_uri is
   *  locked to this value (the new file or text is the document's next
   *  revision) and the title/tags are pre-filled from the prior
   *  document so the user can adjust without retyping everything.
   *  Omit for the standard add-new flow. */
  replaceMode?: {
    sourceUri: string;
    title?: string;
    description?: string;
    tags?: string[];
  };
}

/** What a job the dialog stops following does: it goes on, and a reload shows it (a
 *  replaced document shows its new revision). */
function goesOn(sourceUri: string, replacing: boolean): string {
  const shows = replacing ? "its new revision shows" : "it is in the Library";
  return `${sourceUri} goes on extracting; ${shows} once it publishes and the list is reloaded`;
}

/**
 * Adding a document from the dashboard, two ways:
 *   - A file: its bytes go to `knowledge.upload` as they are, and `knowledge.ingest`
 *     has the server's extractor parse them into the document's next revision. The
 *     browser reads none of it; the badge says beforehand whether this deployment
 *     reads the file's format (`knowledge.formats`), and the job is followed to its end.
 *   - Pasted text: `knowledge.upsert`, which replaces the source's chunks.
 *
 * Open in `replaceMode` to lock the source_uri and pre-fill title +
 * tags from an existing document — used by the Library row's
 * Replace button to publish a new revision.
 */
export function AddDocumentDialog({
  open,
  onOpenChange,
  onBatchStarted,
  identity,
  place,
  onAdded,
  replaceMode,
}: Props) {
  const placeName = place || identity.namespace;
  const [mode, setMode] = useState<"file" | "paste">("file");
  const [file, setFile] = useState<File | null>(null);
  // More than one file picked or dropped: each goes as its own document (BatchUpload).
  const [batch, setBatch] = useState<PickedEntry[] | null>(null);
  // Folders dropped beside the files, which are not read.
  const [droppedFolders, setDroppedFolders] = useState(0);
  // A batch sending: the dialog closes only through its own Close, which asks first.
  const [batchRunning, setBatchRunning] = useState(false);
  // Where the batch's files go, as the batch says: not the place the page shows now.
  const [batchHeading, setBatchHeading] = useState("");
  // The batch's own close, which asks first when files failed.
  const batchClose = useRef<(() => void) | null>(null);
  // What a drop left unused, said beside the form.
  const [note, setNote] = useState("");
  const [sourceUri, setSourceUri] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  const [allowPartial, setAllowPartial] = useState(false);
  // "upload" can be cancelled (nothing is queued yet); "queue" and "save" hold the dialog
  // open (the call may have taken effect); "follow" closes, the job going on on the server.
  const [busy, setBusy] = useState<IngestPhase | "save" | null>(null);
  const [step, setStep] = useState("");
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const following = useRef<AbortController | null>(null);
  // The job being followed, for Stop.
  const [jobId, setJobId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const { formats, settled: formatsRead } = useKnowledgeFormatsRead(identity, open);
  const verdict = file ? fileVerdict(file, formats) : null;
  // Leaving the page stops the following, as closing does; the job goes on on the server.
  useEffect(() => () => following.current?.abort(), []);
  // A file dropped beside the drop zone would open in the tab, leaving the dialog and its
  // batch: while the dialog is up, only the zone takes a file. Text dropped into a field is
  // the browser's, as everywhere.
  useEffect(() => {
    if (!open) return;
    const ignore = (e: DragEvent) => {
      // The zone has already taken a drag over it; elsewhere the cursor says no drop.
      if (e.defaultPrevented || !e.dataTransfer?.types?.includes("Files")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "none";
    };
    window.addEventListener("dragover", ignore);
    window.addEventListener("drop", ignore);
    return () => {
      window.removeEventListener("dragover", ignore);
      window.removeEventListener("drop", ignore);
    };
  }, [open]);

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

  const reset = () => {
    following.current?.abort();
    following.current = null;
    setMode("file");
    setFile(null);
    setBatch(null);
    setBatchRunning(false);
    setBatchHeading("");
    batchClose.current = null;
    setNote("");
    setSourceUri("");
    setTitle("");
    setDescription("");
    setContent("");
    setTagsInput("");
    setAllowPartial(false);
    setError(null);
    setBusy(null);
    setStep("");
    setJobId(null);
  };

  // One file fills the form below; several go as a batch. Replace takes one.
  const onPickFiles = (files: File[], folders = 0) => {
    setDroppedFolders(folders);
    const unread = foldersNotRead(folders);
    const unused = replaceMode && files.length > 1 ? "Replace takes one file; the others were not used." : "";
    setNote([unused, unread].filter(Boolean).join(" "));
    if (files.length > 1 && !replaceMode) {
      setError(null);
      setBatch(files.map((f) => ({ file: f, path: "" })));
      return;
    }
    onPickFile(files[0] ?? null);
  };

  // Files dropped on the picker. A dropped folder is not read (its entries would need the
  // folder APIs); the files beside it are.
  const onDrop = (e: React.DragEvent) => {
    const files: File[] = [];
    let folders = 0;
    // Read now: the browser empties the list once this handler returns.
    for (const item of Array.from(e.dataTransfer.items ?? [])) {
      if (item.kind !== "file") continue;
      if (item.webkitGetAsEntry?.()?.isDirectory) {
        folders++;
        continue;
      }
      const f = item.getAsFile();
      if (f) files.push(f);
    }
    if (!e.dataTransfer.items?.length) files.push(...Array.from(e.dataTransfer.files ?? []));
    if (folders && !files.length) {
      setError("Folders are not taken here: drop or pick the files inside them.");
      return;
    }
    onPickFiles(files, folders);
  };

  const onPickFile = (f: File | null) => {
    if (!f) return;
    setError(null);
    setStep("");
    setFile(f);
    if (!sourceUri) setSourceUri(f.name);
    if (!title) setTitle(titleFromName(f.name));
  };

  const tags = () => tagsFrom(tagsInput);
  const canSubmit =
    sourceUri.trim() !== "" && busy === null && (mode === "file" ? !!file && !verdict?.refusal : content.trim() !== "");

  const finish = (msg: string, level: "ok" | "info" = "ok") => {
    toast(msg, level);
    onAdded();
    reset();
    onOpenChange(false);
  };

  const submitFile = async () => {
    if (!file) return;
    const ctrl = new AbortController();
    following.current = ctrl;
    setBusy("upload");
    try {
      const status = await ingestFile(
        identity,
        file,
        {
          source_uri: sourceUri.trim(),
          title: title.trim(),
          description: description.trim(),
          tags: tags(),
          allow_partial: allowPartial,
        },
        (line, phase) => {
          setStep(line);
          setBusy(phase);
        },
        { signal: ctrl.signal, onQueued: setJobId },
      );
      if (status.state === "ready" || status.state === "partial") {
        const what = `${replaceMode ? "Replaced" : "Added"} ${sourceUri.trim()}: revision ${status.revision}`;
        if (status.state === "ready") finish(what);
        else {
          const lost = status.coverage?.lost?.length ? ` (lost: ${status.coverage.lost.join(", ")})` : "";
          finish(`${what}, partial: part of the file could not be read${lost}`, "info");
        }
        return;
      }
      // The reason once, in the alert: the status line would repeat it.
      setStep("");
      setError(status.found ? `${file.name}: ${ingestStep(status)}` : "The ingest job was not found.");
    } catch (e) {
      if (ctrl.signal.aborted) return; // closed while following: the job goes on
      if (e instanceof FollowLost) {
        // Queued, but no longer heard of: say it goes on rather than offer to send it again.
        toast(goesOn(sourceUri.trim(), !!replaceMode), "info");
        reset();
        onOpenChange(false);
        return;
      }
      setError(humanizeError(e));
    }
    setBusy(null);
  };

  const submitText = async () => {
    setBusy("save");
    try {
      const r = await addKnowledge(identity, {
        source_uri: sourceUri.trim(),
        title: title.trim(),
        description: description.trim(),
        content,
        tags: tags(),
      });
      // Heads-up when this text duplicates an existing doc under a different source_uri.
      if (r.duplicate_of_uri && r.duplicate_of_uri !== sourceUri.trim()) {
        toast(`Same content already in your library as "${r.duplicate_of_uri}" — keeping both`, "info");
      }
      finish(replaceMode ? `Replaced ${replaceMode.sourceUri}` : "Document added");
    } catch (e) {
      setError(humanizeError(e));
      setBusy(null);
    }
  };

  const onSubmit = async () => {
    if (!canSubmit) return;
    setError(null);
    await (mode === "file" ? submitFile() : submitText());
  };

  // Closing during the upload cancels it (nothing is queued yet); the ingest call, a text
  // save and a Stop hold the dialog; once the job is followed it goes on without the dialog.
  const held = busy === "queue" || busy === "save" || stopping || batchRunning;
  const close = (v: boolean) => {
    if (held && !v) return;
    if (!v && batch && batchClose.current) {
      batchClose.current();
      return;
    }
    if (!v && busy === "follow") toast(goesOn(sourceUri.trim(), !!replaceMode), "info");
    if (!v) reset();
    onOpenChange(v);
  };
  // Stop the job being followed: knowledge.ingest_cancel. One that ended meanwhile says how,
  // and one that published is added like any other.
  const stop = async () => {
    if (!jobId) return;
    // The Stop's answer decides: the follow no longer finishes the dialog on its own.
    following.current?.abort();
    setStopping(true);
    try {
      const r = await cancelIngestion(identity, jobId);
      if (published(r)) onAdded();
      toast(stopped(sourceUri.trim(), r), "info");
    } catch (e) {
      // The follow is stopped and the cancel may or may not have landed.
      toastError(`Stop failed (${sourceUri.trim()} may still be extracting)`, e);
    } finally {
      setStopping(false);
      reset();
      onOpenChange(false);
    }
  };
  const readable = readableFormats(formats);

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title={
        replaceMode ? "Replace content" : batch ? `Add documents ${batchHeading || `to ${placeName}`}` : "Add document"
      }
      description={replaceMode?.sourceUri}
      className="w-full max-w-[640px]"
    >
      {batch ? (
        <BatchUpload
          identity={identity}
          place={placeName}
          entries={batch}
          folders={droppedFolders}
          formats={formats}
          formatsRead={formatsRead}
          tags={tagsInput}
          partial={allowPartial}
          onAdded={onAdded}
          onRunning={setBatchRunning}
          onStarted={onBatchStarted}
          onDestination={setBatchHeading}
          closeWith={(c) => {
            batchClose.current = c;
          }}
          onClose={() => {
            reset();
            onOpenChange(false);
          }}
        />
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void onSubmit();
          }}
        >
          <div className="space-y-4">
            <Tabs value={mode} onValueChange={(v) => busy === null && setMode(v === "paste" ? "paste" : "file")}>
              <Tabs.TriggersList aria-label="How to add it">
                <TabTrigger value="file">Upload a file</TabTrigger>
                <TabTrigger value="paste">Paste text</TabTrigger>
              </Tabs.TriggersList>
              <Tabs.Content value="file" className="space-y-2 pt-4">
                <input
                  aria-label="Choose a file to upload"
                  ref={fileRef}
                  type="file"
                  multiple={!replaceMode}
                  className="hidden"
                  onChange={(e) => {
                    onPickFiles(Array.from(e.target.files ?? []));
                    e.target.value = ""; // the same files picked again still fire
                  }}
                />
                <DropZone data-testid="add-doc-drop" busy={busy !== null} onDrop={onDrop} className="space-y-2 p-3">
                  {file && verdict && <PickedFile file={file} verdict={verdict} />}
                  <div className="flex flex-wrap items-center justify-center gap-2">
                    {!replaceMode && <span className="drop-hint text-sm text-text-muted">Drop files here, or</span>}
                    <Button
                      type="secondary"
                      htmlType="button"
                      iconLeft="upload"
                      disabled={busy !== null}
                      onClick={() => fileRef.current?.click()}
                    >
                      {file ? "Choose another file" : replaceMode ? "Choose the new file" : "Choose files"}
                    </Button>
                  </div>
                </DropZone>
                <p className="text-xs text-text-muted">
                  The server reads the file{readable ? ` (${readable})` : ""}; its text shows in the Library once the
                  ingest publishes.
                </p>
                {verdict?.refusal && <Alert role="alert" state="danger" text={verdict.refusal} />}
                {note && <Alert state="info" text={note} />}
                <AllowPartial checked={allowPartial} onChange={setAllowPartial} disabled={busy !== null} />
              </Tabs.Content>
              <Tabs.Content value="paste" className="pt-4">
                <TextAreaField
                  id="add-doc-content"
                  disabled={busy !== null}
                  label="Content"
                  value={content}
                  onChange={setContent}
                  placeholder="Paste the document text here. Loom chunks + embeds on upload."
                  rows={10}
                />
                <div className="mt-1 text-xs text-text-muted tabular">{content.length.toLocaleString()} chars</div>
              </Tabs.Content>
            </Tabs>
            {/* Locked in Replace mode, so the new content is the document's next revision
              rather than a second document under another URI. */}
            {!replaceMode && (
              <TextField
                id="add-doc-source-uri"
                disabled={busy !== null}
                label="Source URI (stable identifier the agent cites — a path, URL, or slug)"
                value={sourceUri}
                onChange={setSourceUri}
                placeholder="docs/getting-started/quickstart.md"
              />
            )}
            <TextField
              id="add-doc-title"
              disabled={busy !== null}
              label="Title (optional — overrides filename in the Library list)"
              value={title}
              onChange={setTitle}
              placeholder="Onboarding guide"
            />
            <TextAreaField
              id="add-doc-description"
              disabled={busy !== null}
              label="Description (optional — one-line summary; improves retrieval and shows in the Library list)"
              value={description}
              onChange={setDescription}
              placeholder="What's in this document, in one or two sentences."
              rows={2}
            />
            <TextField
              id="add-doc-tags"
              disabled={busy !== null}
              label="Tags (comma-separated, optional)"
              value={tagsInput}
              onChange={setTagsInput}
              placeholder="reference, work"
            />
            <IngestProgress step={step} />
            {error && <Alert role="alert" state="danger" text={error} />}
          </div>
          <ModalFooter>
            <Button type="secondary" htmlType="button" disabled={held} onClick={() => close(false)}>
              {busy === "follow" ? "Close" : "Cancel"}
            </Button>
            {busy === "follow" && jobId && (
              <Button
                type="danger"
                htmlType="button"
                loading={stopping}
                disabled={stopping}
                onClick={() => void stop()}
              >
                Stop ingestion
              </Button>
            )}
            <Button type="primary" htmlType="submit" disabled={!canSubmit} loading={busy !== null}>
              {replaceMode ? "Save changes" : mode === "file" ? "Upload" : "Add"}
            </Button>
          </ModalFooter>
        </form>
      )}
    </Modal>
  );
}

/** What a Stop came to: a running job only stops before it publishes, and one that ended
 *  meanwhile stays as it ended. */
function stopped(uri: string, r: IngestCancelResult): string {
  if (!r.found) return `${uri} has no ingestion to stop`;
  if (r.cancelled) return r.state === "processing" ? `Stopping ${uri} before it publishes` : `Stopped ${uri}`;
  if (published(r)) return `${uri} was already added`;
  if (r.state === "failed") return `${uri} had already failed`;
  return `${uri} was already stopped`;
}

const published = (r: IngestCancelResult) => r.found && (r.state === "ready" || r.state === "partial");
