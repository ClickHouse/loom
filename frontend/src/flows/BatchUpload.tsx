import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import type { Identity, KnowledgeFormats } from "../api";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { Alert } from "../components/ui/Alert";
import { ChoiceGroup } from "../components/ui/ChoiceGroup";
import { ModalFooter } from "../components/ui/Modal";
import { Unavailable } from "../components/ui/Unavailable";
import {
  BATCH_OPEN,
  type BatchRow,
  capBatch,
  existingSources,
  foldersNotRead,
  MAX_BATCH_BYTES,
  MAX_BATCH_FILES,
  MAX_CHECKED,
  type PickedEntry,
  planBatch,
  retryFailed,
  runBatch,
} from "../lib/batchIngest";
import { errorLine } from "../lib/errorWords";
import { tagsFrom } from "../lib/fileIngest";
import { formatBytesShort } from "../lib/format";
import { AllowPartial } from "./FileIngest";

/** A short name per state, so no two read the same. */
const BADGE: Record<BatchRow["state"], { text: string; state: "success" | "danger" | "warning" | "neutral" | "info" }> =
  {
    waiting: { text: "Waiting", state: "neutral" },
    sending: { text: "Uploading", state: "info" },
    working: { text: "Processing", state: "info" },
    done: { text: "Done", state: "success" },
    partial: { text: "Partly added", state: "warning" },
    failed: { text: "Failed", state: "danger" },
    skipped: { text: "Skipped", state: "neutral" },
    not_sent: { text: "Not sent", state: "neutral" },
    next: { text: "Next upload", state: "neutral" },
    goes_on: { text: "Still processing", state: "info" },
  };

/** What a batch came to, in one line. */
export function batchSummary(rows: BatchRow[]): string {
  const n = (s: BatchRow["state"][]) => rows.filter((r) => s.includes(r.state)).length;
  const parts = [
    [n(["done", "partial"]), "added"],
    [n(["goes_on", "working"]), "still processing"],
    [n(["failed"]), "failed"],
    [n(["skipped"]), "skipped"],
    [n(["not_sent", "next", "waiting", "sending"]), "not sent"],
  ] as const;
  return parts
    .filter(([c]) => c > 0)
    .map(([c, w]) => `${c} ${w}`)
    .join(", ");
}

/** "8 of 12 sent · 1 failed": the one line a screen reader hears while a batch runs. */
export function progressLine(rows: BatchRow[]): string {
  const counted = rows.filter((r) => r.state !== "skipped" && r.state !== "next");
  const n = (s: BatchRow["state"][]) => counted.filter((r) => s.includes(r.state)).length;
  const sent = n(["done", "partial", "working", "goes_on"]);
  const failed = n(["failed"]);
  const notSent = n(["not_sent"]);
  return `${sent} of ${counted.length} sent${failed ? ` · ${failed} failed` : ""}${notSent ? ` · ${notSent} not sent` : ""}`;
}

type Choice = "replace" | "skip";

/** A batch most of whose files to send are already here is most likely sent again: Skip is
 *  picked first then, unless the person has picked. */
function firstChoice(rows: BatchRow[]): Choice {
  const waiting = rows.filter((r) => r.state === "waiting" || r.state === "next");
  return waiting.filter((r) => r.replaces).length * 2 > waiting.length ? "skip" : "replace";
}

/** One file's line. Memoised: Click UI's Badge measures its text on every render, and the
 *  list redraws on each step of up to three open jobs, while a row changes only with its own. */
const FileRow = memo(function FileRow({ row, place, choice }: { row: BatchRow; place: string; choice: Choice }) {
  return (
    <li className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <span className="min-w-0 flex-1 break-all font-medium">{row.key}</span>
      <span className="text-text-muted tabular">{formatBytesShort(row.file.size)}</span>
      <Badge size="sm" state={BADGE[row.state].state} text={BADGE[row.state].text} />
      <span className="w-full text-xs text-text-muted">
        {row.state === "waiting" && row.replaces
          ? `Already in ${place}: ${choice === "skip" ? "skipped" : "replaced"}`
          : row.line}
      </span>
    </li>
  );
});

/**
 * Many files into one place: each its own upload and ingest, `BATCH_OPEN` at a time, with
 * a line per file saying where it is. Files already there are replaced or skipped, as the
 * person picks; Retry failed sends the failed ones again; Stop cancels the jobs in flight
 * and sends no more.
 */
export function BatchUpload({
  identity: identityProp,
  place: placeProp,
  entries,
  folders = 0,
  formats,
  formatsRead,
  tags = "",
  partial = false,
  onAdded,
  onRunning,
  onStarted,
  onDestination,
  closeWith,
  onClose,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  /** What the place is called on screen. */
  place: string;
  entries: PickedEntry[];
  /** Folders dropped beside the files, which are not read. */
  folders?: number;
  formats: KnowledgeFormats | null;
  /** Whether the formats read has answered (`formats` null then: it could not be read). */
  formatsRead: boolean;
  /** What the one-file form held when several files were picked: the batch starts with it. */
  tags?: string;
  partial?: boolean;
  /** Some file was added: the Library reloads. */
  onAdded: () => void;
  /** Whether files are being sent: the dialog holds until they are, or Stop. */
  onRunning?: (running: boolean) => void;
  /** Whether the batch has started: from then the page keeps the dialog open, so what
   *  failed or waits is closed only through Done, which asks first. */
  onStarted?: (started: boolean) => void;
  /** Where the files go, in the words the dialog's heading uses ("to Team"). */
  onDestination?: (words: string) => void;
  /** Hands the dialog this batch's own close (it asks first when files failed), for the
   *  modal's close button and Escape. */
  closeWith?: (close: () => void) => void;
  onClose: () => void;
}) {
  // Where the files go, as it was when the dialog opened: the place the page shows may
  // change meanwhile (Back), and these files still go where they were picked for.
  const [identity] = useState(identityProp);
  const [place] = useState(placeProp);
  // Held to one upload's limits from the first render, before the check answers.
  const [rows, setRows] = useState<BatchRow[]>(() =>
    capBatch(
      planBatch(entries, formats, () => identity),
      false,
    ),
  );
  // "partial": too many documents to read them all; "failed": why is in `checkError`.
  const [checked, setChecked] = useState<"checking" | "ok" | "partial" | "failed">("checking");
  const [checkError, setCheckError] = useState("");
  const [checks, setChecks] = useState(0);
  // Set by the first Upload: a run that only skipped files has started all the same.
  const [started, setStarted] = useState(false);
  const [choice, setChoice] = useState<Choice>("replace");
  // The person's own Replace or Skip, which a later check does not undo.
  const chosen = useRef<Choice | null>(null);
  // The formats may arrive after the pick: what is left out is decided again then, while
  // nothing has been sent, each file keeping what the check found of it.
  const [plannedWith, setPlannedWith] = useState(formats);
  if (formats !== plannedWith && !started) {
    setPlannedWith(formats);
    const found = new Map(rows.map((r) => [r.sourceUri, r]));
    const replanned = planBatch(entries, formats, () => identity).map((r) => {
      const was = found.get(r.sourceUri);
      return was ? { ...r, replaces: was.replaces, kept: was.kept } : r;
    });
    const use = chosen.current ?? firstChoice(replanned);
    setChoice(use);
    setRows(capBatch(replanned, use === "skip"));
  }
  const [page, setPage] = useState(1);
  const [tagsInput, setTagsInput] = useState(tags);
  const [allowPartial, setAllowPartial] = useState(partial);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [status, setStatus] = useState("");
  const ctrl = useRef<AbortController | null>(null);
  const cancelling = useRef(false);
  const retryRef = useRef<HTMLButtonElement | null>(null);
  const uploadRef = useRef<HTMLButtonElement | null>(null);
  // The form the files were picked in goes as this list replaces it: Upload takes the focus.
  useEffect(() => {
    const raf = requestAnimationFrame(() => uploadRef.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, []);
  const checkAgainRef = useRef<HTMLButtonElement | null>(null);
  // Check again goes while its read runs: the focus then goes to Upload, or back to it.
  const refocus = useRef(false);
  const doneRef = useRef<HTMLButtonElement | null>(null);
  const statusRef = useRef<HTMLParagraphElement | null>(null);
  const latest = useRef(rows);
  latest.current = rows;

  // What is already there: those files are replaced or skipped. Past MAX_CHECK_PAGES the
  // rest is not looked for, and a file whose source exists is skipped (a new source
  // publishes only while none is live). The upload waits for a check that succeeds.
  const { org, namespace } = identity;
  // Each change of `checks` (Check again) reads again.
  useEffect(() => {
    const c = new AbortController();
    setChecked("checking");
    existingSources({ org, namespace }, { signal: c.signal, onPage: setPage })
      .then(({ uris, complete }) => {
        const marked = latest.current.map((r) => ({
          ...r,
          replaces: uris.has(r.sourceUri),
          kept: uris.get(r.sourceUri),
        }));
        const use = chosen.current ?? firstChoice(marked);
        setChoice(use);
        setRows(capBatch(marked, use === "skip"));
        setChecked(complete ? "ok" : "partial");
        if (refocus.current) requestAnimationFrame(() => uploadRef.current?.focus());
        refocus.current = false;
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        // A list only reads: nothing can have been saved.
        setCheckError(errorLine(e, { read: true }));
        setChecked("failed");
        if (refocus.current) requestAnimationFrame(() => checkAgainRef.current?.focus());
        refocus.current = false;
      });
    return () => c.abort();
  }, [org, namespace, checks]);

  const pick = (next: Choice) => {
    chosen.current = next;
    setChoice(next);
    setRows((rs) => capBatch(rs, next === "skip"));
  };

  // Leaving the page while files are not yet sent drops them: the browser asks first.
  const unsent = rows.some((r) => r.state === "waiting" || r.state === "sending");
  useEffect(() => {
    if (!running || !unsent) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [running, unsent]);
  // Whether a run is still going, for a Stop confirmed after it ended.
  const runningRef = useRef(false);
  runningRef.current = running;
  // The page closing mid-upload sends no more; the run, once it ends, says what that left.
  const closedPage = useRef(false);
  useEffect(() => {
    // Set again on mount: StrictMode runs this cleanup once before the real mount.
    closedPage.current = false;
    return () => {
      closedPage.current = true;
      ctrl.current?.abort();
    };
  }, []);
  useEffect(() => onRunning?.(running), [running, onRunning]);
  // The status line, at most once a second: twelve rows changing would flood a reader.
  useEffect(() => {
    if (!running) return;
    setStatus(progressLine(latest.current));
    const t = setInterval(() => setStatus(progressLine(latest.current)), 1000);
    return () => clearInterval(t);
  }, [running]);

  const held = rows.filter((r) => r.state === "next").length;
  const existing = rows.filter((r) => r.replaces && (r.state === "waiting" || r.state === "next")).length;
  const toSend = rows.filter((r) => r.state === "waiting" && !(choice === "skip" && r.replaces)).length;
  const failed = rows.filter((r) => r.state === "failed").length;
  const bytes = useMemo(
    () =>
      rows
        .filter((r) => r.state === "waiting" && !(choice === "skip" && r.replaces))
        .reduce((n, r) => n + r.file.size, 0),
    [rows, choice],
  );

  // `only`: send again the rows in that state (the failed ones), or, for "next", the ones
  // held past the last upload's limits, capped again.
  const send = async (only?: BatchRow["state"]) => {
    const c = new AbortController();
    ctrl.current = c;
    cancelling.current = false;
    const start =
      only === "next"
        ? capBatch(latest.current, choice === "skip")
        : only === "failed"
          ? retryFailed(latest.current, choice === "skip")
          : latest.current;
    // Under "Skip them" the files already there end now, so the count of what is sent
    // holds still while the batch runs.
    const skipping = choice === "skip";
    const begin = start.map((r) =>
      skipping && r.state === "waiting" && r.replaces ? { ...r, state: "skipped" as const, line: "Already here" } : r,
    );
    setRows(begin);
    setStarted(true);
    setRunning(true);
    requestAnimationFrame(() => statusRef.current?.focus());
    const tags = tagsFrom(tagsInput);
    // Each row as the run left it, kept here: the rendered rows may lag the last update.
    const final = new Map(begin.map((r) => [r.id, r]));
    await runBatch(begin, {
      fields: { tags, allow_partial: allowPartial },
      signal: c.signal,
      cancelling: () => cancelling.current,
      onRow: (row) => {
        final.set(row.id, row);
        setRows((rs) => rs.map((r) => (r.id === row.id ? row : r)));
      },
    });
    setRunning(false);
    setStopping(false);
    const end = [...final.values()];
    setStatus(progressLine(end));
    if (closedPage.current) {
      toast(`Upload stopped when the page closed: ${batchSummary(end)}`, "info");
      return;
    }
    // A file skipped during the run met a live document the check did not see (published
    // meanwhile, or past the checked documents): it may be new to the list.
    const sent = new Set(begin.filter((r) => r.state === "waiting").map((r) => r.id));
    const sentSkipped = end.some((r) => r.state === "skipped" && sent.has(r.id));
    if (sentSkipped || end.some((r) => r.state === "done" || r.state === "partial")) onAdded();
    toast(batchSummary(end), end.some((r) => r.state === "failed") || c.signal.aborted ? "info" : "ok");
    // The next thing to do: send the failed ones again, or finish.
    requestAnimationFrame(() => (end.some((r) => r.state === "failed") ? retryRef : doneRef).current?.focus());
  };

  const stop = async () => {
    const ok = await confirmAction({
      title: "Stop sending?",
      message:
        "Files not yet sent are not sent, and the ones Loom is processing are asked to stop; one already publishing may still be added.",
      confirmLabel: "Stop",
      danger: true,
    });
    // The run may have ended while the confirm was open: nothing is left to stop then.
    if (!ok || !runningRef.current) return;
    setStopping(true);
    cancelling.current = true;
    ctrl.current?.abort();
  };

  // Closing drops what failed and what waits for the next upload: it asks first.
  const done = async () => {
    if (failed > 0 || held > 0) {
      const lost = [
        failed ? `${failed} file${failed === 1 ? "" : "s"} failed` : "",
        held ? `${held} ${held === 1 ? "is" : "are"} not sent yet` : "",
      ]
        .filter(Boolean)
        .join(" and ");
      const ok = await confirmAction({
        title: "Close with files not added?",
        message: `${lost}. Close anyway?`,
        confirmLabel: "Close",
      });
      if (!ok) return;
    }
    onClose();
  };
  // What "Upload the next files" sends: the next upload's share, by count and by size.
  const nextCount = useMemo(
    () => (held ? capBatch(rows, choice === "skip").filter((r) => r.state === "waiting").length : 0),
    [rows, choice, held],
  );
  // What Retry failed sends: as many of the failed as one upload takes.
  const retryCount = useMemo(
    () => (failed ? retryFailed(rows, choice === "skip").filter((r) => r.state === "waiting").length : 0),
    [rows, choice, failed],
  );

  const finish = started ? () => void done() : onClose;
  useEffect(() => onStarted?.(started), [started, onStarted]);
  const destination = `to ${place}`;
  useEffect(() => onDestination?.(destination), [destination, onDestination]);
  useEffect(() => closeWith?.(finish));

  const checking = checked === "checking";
  const checkFailed = checked === "failed";
  const upload = (
    <Button ref={uploadRef} type="primary" htmlType="button" onClick={() => void send()}>
      {`Upload ${toSend} file${toSend === 1 ? "" : "s"} ${destination}`}
    </Button>
  );
  return (
    <div className="space-y-4">
      {!started && (
        <p className="text-sm text-text-muted">
          {`${toSend} file${toSend === 1 ? "" : "s"} · ${formatBytesShort(bytes)}, each its own document. ${BATCH_OPEN} are sent at a time, so agents adding to ${place} are not held up; keep this open until they are sent.`}
        </p>
      )}
      {folders > 0 && <Alert state="info" text={foldersNotRead(folders)} />}
      {/* Once started, the files go as they were planned, whatever a later read says. */}
      {!started && formatsRead && !formats && (
        <Alert
          state="warning"
          text="Couldn't read which formats this deployment reads: every file is sent, and one it can't read fails on its own."
        />
      )}
      {checking && (
        <p className="text-xs text-text-muted">{`Checking which files are already in ${place}${page > 1 ? ` (page ${page})` : ""}…`}</p>
      )}
      {checked === "partial" && (
        <Alert
          state="warning"
          text={`${place} holds over ${MAX_CHECKED.toLocaleString()} documents, so only the first ${MAX_CHECKED.toLocaleString()} were checked. A file that matches an unchecked document is skipped, not replaced.`}
        />
      )}
      {checkFailed && (
        <div className="space-y-2">
          <Alert state="warning" text={`Couldn't check which files are already in ${place}: ${checkError}`} />
          <Button
            ref={checkAgainRef}
            type="secondary"
            htmlType="button"
            onClick={() => {
              refocus.current = true;
              setChecks((n) => n + 1);
            }}
          >
            Check again
          </Button>
        </div>
      )}
      {held > 0 && (
        <Alert
          // Before the first upload it only says how the files go; after it, Done drops them.
          state={started ? "warning" : "info"}
          text={
            started
              ? `${held} file${held === 1 ? " waits" : "s wait"} for the next upload (at most ${MAX_BATCH_FILES} files and ${MAX_BATCH_BYTES / 1024 ** 3} GB at a time).`
              : `One upload sends at most ${MAX_BATCH_FILES} files and ${MAX_BATCH_BYTES / 1024 ** 3} GB at a time. The first ${toSend} are sent now; then send the next ones from here.`
          }
        />
      )}
      {!started && existing > 0 && (
        <ChoiceGroup
          label={`${existing} file${existing === 1 ? " is" : "s are"} already in ${place}`}
          value={choice}
          onChange={pick}
          options={[
            {
              value: "replace",
              label: "Replace them with these files",
              hint: "Each keeps its title, description and tags; the old version stays in its history.",
            },
            {
              value: "skip",
              label: "Skip them",
              hint: "Picked first when most of the files to send are already there.",
            },
          ]}
        />
      )}
      {!started && (
        <>
          <TextField
            id="batch-tags"
            label="Tags for every file (comma-separated, optional)"
            value={tagsInput}
            onChange={setTagsInput}
            placeholder="reference, work"
          />
          <AllowPartial checked={allowPartial} onChange={setAllowPartial} />
        </>
      )}
      {started && (
        <p ref={statusRef} tabIndex={-1} role="status" className="text-sm tabular">
          {status}
        </p>
      )}
      <ul
        aria-label="Files"
        // biome-ignore lint/a11y/noNoninteractiveTabindex: the list scrolls and holds nothing focusable, so a keyboard scrolls it from here
        tabIndex={0}
        className="max-h-80 divide-y divide-border overflow-y-auto rounded-md border border-border"
      >
        {rows.map((r) => (
          <FileRow key={r.id} row={r} place={place} choice={choice} />
        ))}
      </ul>
      <ModalFooter>
        {!started && (
          <Button type="secondary" htmlType="button" onClick={onClose}>
            Cancel
          </Button>
        )}
        {running && (
          <Button type="danger" htmlType="button" loading={stopping} disabled={stopping} onClick={() => void stop()}>
            Stop
          </Button>
        )}
        {!running && failed > 0 && (
          <Button ref={retryRef} type="secondary" htmlType="button" onClick={() => void send("failed")}>
            {`Retry ${retryCount} failed`}
          </Button>
        )}
        {started && !running && held > 0 && (
          <Button type="secondary" htmlType="button" onClick={() => void send("next")}>
            {`Upload the next ${nextCount} file${nextCount === 1 ? "" : "s"}`}
          </Button>
        )}
        {started && !running && (
          <Button ref={doneRef} type="primary" htmlType="button" onClick={() => void done()}>
            Done
          </Button>
        )}
        {!started && (
          // Always the same wrapper, never disabled: the button keeps its focus when the check
          // ends, and takes the focus Check again hands it.
          <Unavailable
            reason={
              // Until the deployment says which formats it reads, nothing is sent: an unread
              // one would go as the default format.
              !formatsRead
                ? "Reading which formats this deployment reads…"
                : checking
                  ? `Checking which files are already in ${place}…`
                  : checkFailed
                    ? "Check again first."
                    : toSend === 0
                      ? "No file to send: every one is left out or skipped."
                      : null
            }
          >
            {upload}
          </Unavailable>
        )}
      </ModalFooter>
    </div>
  );
}
