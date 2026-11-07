import { useEffect, useRef, useState } from "react";
import { Alert } from "../components/ui/Alert";
import { Button } from "@clickhouse/click-ui/Button";
import { Tabs } from "@clickhouse/click-ui/Tabs";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { addKnowledge, cancelIngestion, type Identity, type IngestCancelResult } from "../api";
import { toast } from "../components/Toast";
import { Modal, ModalFooter } from "../components/ui/Modal";
import { TabTrigger } from "../components/ui/TabTrigger";
import { humanizeError } from "../lib/format";
import { FollowLost, fileVerdict, type IngestPhase, ingestFile, ingestStep } from "../lib/fileIngest";
import { AllowPartial, IngestProgress, PickedFile, readableFormats, useKnowledgeFormats } from "./FileIngest";

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Pick<Identity, "org" | "namespace">;
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
export function AddDocumentDialog({ open, onOpenChange, identity, onAdded, replaceMode }: Props) {
  const [mode, setMode] = useState<"file" | "paste">("file");
  const [file, setFile] = useState<File | null>(null);
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
  const formats = useKnowledgeFormats(identity, open);
  const verdict = file ? fileVerdict(file, formats) : null;
  // Leaving the page stops the following, as closing does; the job goes on on the server.
  useEffect(() => () => following.current?.abort(), []);

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

  const onPickFile = (f: File | null) => {
    if (!f) return;
    setError(null);
    setStep("");
    setFile(f);
    if (!sourceUri) setSourceUri(f.name);
    if (!title) setTitle(f.name.replace(/\.[^.]+$/, ""));
  };

  const tags = () =>
    tagsInput
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
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
  const held = busy === "queue" || busy === "save" || stopping;
  const close = (v: boolean) => {
    if (held && !v) return;
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
      reset();
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setStopping(false);
    }
  };
  const holdWhileBusy = (e: Event) => {
    if (held) e.preventDefault();
  };
  const readable = readableFormats(formats);

  return (
    <Modal
      open={open}
      onOpenChange={close}
      title={replaceMode ? "Replace content" : "Add document"}
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
                className="hidden"
                onChange={(e) => onPickFile(e.target.files?.[0] || null)}
              />
              {file && verdict && <PickedFile file={file} verdict={verdict} />}
              <Button
                type="secondary"
                htmlType="button"
                iconLeft="upload"
                fillWidth
                disabled={busy !== null}
                onClick={() => fileRef.current?.click()}
              >
                {file ? "Pick another file" : replaceMode ? "Pick the new file" : "Pick a file"}
              </Button>
              <p className="text-xs text-text-muted">
                The server reads the file{readable ? ` (${readable})` : ""}; its text shows in the Library once the
                ingest publishes.
              </p>
              {verdict?.refusal && <Alert role="alert" state="danger" text={verdict.refusal} />}
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
            <Button type="danger" htmlType="button" loading={stopping} disabled={stopping} onClick={() => void stop()}>
              Stop ingestion
            </Button>
          )}
          <Button type="primary" htmlType="submit" disabled={!canSubmit} loading={busy !== null}>
            {replaceMode ? "Save changes" : mode === "file" ? "Upload" : "Add"}
          </Button>
        </ModalFooter>
      </form>
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
