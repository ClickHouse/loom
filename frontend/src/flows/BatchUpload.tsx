import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import type { CollectionEntry, Identity, KnowledgeFormats } from "../api";
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
  inTurn,
  type KeptFields,
  MAX_BATCH_BYTES,
  MAX_BATCH_FILES,
  MAX_CHECKED,
  type PickedEntry,
  planBatch,
  quietRetry,
  retryFailed,
  runBatch,
} from "../lib/batchIngest";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { errorLine } from "../lib/errorWords";
import { tagsFrom } from "../lib/fileIngest";
import { formatBytesShort } from "../lib/format";
import {
  afterPlacing,
  collectionsMade,
  FolderCollections,
  type FolderPlan,
  folderLabel,
  folderOf,
  MAX_DEPTH,
  MAX_FOLDERS,
  placeFolders,
} from "../lib/folderUpload";
import { AllowPartial } from "./FileIngest";
import { useFolderPlan } from "./useFolderPlan";

/** Collections read only in part, in words. */
function cutLine(names: string[]): string {
  const n = MAX_CHECKED.toLocaleString();
  return `${names.join(", ")} ${names.length === 1 ? "holds" : "each hold"} over ${n} documents, so only the first ${n} were checked. A file that matches an unchecked document is skipped, not replaced.`;
}

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
function firstChoice(rows: BatchRow[], twins: ReadonlySet<number>): Choice {
  const waiting = rows.filter((r) => (r.state === "waiting" || r.state === "next") && !twins.has(r.id));
  return waiting.filter((r) => r.replaces).length * 2 > waiting.length ? "skip" : "replace";
}

/** A folder's file's line while its collection is made, before any file goes. */
const MAKING = "Making its collection…";

/** The folder files the check predicts into the same collection as an earlier one of their
 *  name (`where`): twins the upload skips, so they are counted once. */
function twinIds(rows: BatchRow[], where: (r: BatchRow) => string): Set<number> {
  const seen = new Set<string>();
  const out = new Set<number>();
  // The one this upload sends is kept over one held for the next, as the upload keeps it.
  for (const state of ["waiting", "next"] as const)
    for (const r of rows) {
      if (r.state !== state || !r.folder || !where(r)) continue;
      const key = `${where(r)}\0${r.sourceUri}`;
      if (seen.has(key)) out.add(r.id);
      else seen.add(key);
    }
  return out;
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

/** Lines of the folder preview before "and N more". */
const PREVIEW_LINES = 10;

/** One folder's line in the preview: its path by collection names, its files, and whether
 *  its collection is there already. */
function previewLine(p: FolderPlan, label: (path: string) => string): string {
  const parts = p.path.split("/");
  const path = parts.map((_, i) => label(parts.slice(0, i + 1).join("/"))).join(" › ");
  const files = p.files ? ` · ${p.files} file${p.files === 1 ? "" : "s"}` : "";
  const there = p.existing ? (p.files ? " · exists — files added to it" : " · exists") : "";
  return `${path}${files}${there}`;
}

/** What a folder upload will make: each folder, nested, with its files, and whether its
 *  collection is there already. */
function FolderPreview({
  plans,
  place,
  label,
}: {
  plans: FolderPlan[];
  /** "in Team" for the collection shown; "" for the top level. */
  place: string;
  label: (path: string) => string;
}) {
  const made = plans.filter((p) => !p.existing).length;
  // At the top level only the dropped folders sit there; the folders inside them nest.
  const tops = plans.filter((p) => !p.existing && !p.parentPath).length;
  const where = place ? ` ${place}` : tops === made ? " at the top level" : tops ? `, ${tops} at the top level` : "";
  const shown = plans.slice(0, PREVIEW_LINES);
  return (
    <div className="space-y-1 text-sm">
      <p>
        {made
          ? `Creates ${made} collection${made === 1 ? "" : "s"}${where}:`
          : `Adds the files to collections already ${place || "at the top level"}:`}
      </p>
      <ul aria-label="Collections the folders go to" className="space-y-0.5 text-text-muted">
        {shown.map((p) => (
          <li key={p.path}>{previewLine(p, label)}</li>
        ))}
        {plans.length > PREVIEW_LINES && <li>{`and ${plans.length - PREVIEW_LINES} more`}</li>}
      </ul>
    </div>
  );
}

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
  foldersLater = false,
  readNote = "",
  formats,
  formatsRead,
  tags = "",
  partial = false,
  onAdded,
  onRunning,
  onStarted,
  onDestination,
  closeWith,
  tree: treeProp,
  onClose,
}: {
  identity: Pick<Identity, "org" | "namespace">;
  /** What the place is called on screen. */
  place: string;
  entries: PickedEntry[];
  /** Folders dropped beside the files, which are not read. */
  folders?: number;
  /** Those folders wait for the Library's whole list of collections. */
  foldersLater?: boolean;
  /** What reading a folder left out (hidden folders, unreadable items), in words. */
  readNote?: string;
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
  /** The collections the person reads and the one shown (null for a namespace), for
   *  picked folders to become collections; none means folders keep only their paths. */
  tree?: { rows: CollectionEntry[]; shown: CollectionEntry | null };
  onClose: () => void;
}) {
  // Where the files go, as it was when the dialog opened: the Library reads its list
  // again after an upload, and the place it shows meanwhile is not where these files go.
  const [identity] = useState(identityProp);
  const { org, namespace } = identity;
  const [place] = useState(placeProp);
  const [tree] = useState(treeProp);
  // Folders become collections inside the collection shown; on "My files" or a namespace,
  // the person picks: a new top-level collection per folder, or the files kept here with
  // their folder paths.
  const plan = (mode: boolean, readable: KnowledgeFormats | null) =>
    planBatch(
      entries,
      readable,
      () => identity,
      (e) =>
        mode && e.path.includes("/")
          ? { folder: folderOf(e.path), sourceUri: e.file.name }
          : { folder: "", sourceUri: e.path || e.file.name },
    );
  // The formats the files were last planned with: they change only before the first upload.
  const [plannedWith, setPlannedWith] = useState(formats);
  // The files of folders that are sent, as folders: one left out (by its format) makes no
  // collection. `plan` reads the pick, which a dialog never changes.
  const foldersIn = (readable: KnowledgeFormats | null) =>
    plan(true, readable).filter((r) => r.folder && r.state !== "skipped");
  const folderFiles = useMemo(() => foldersIn(plannedWith), [plannedWith]);
  const nestable = !!tree?.shown && !tree.shown.personal;
  // Whether the folders go as collections: on My files or a namespace, as the person picks.
  const asCollectionsFor = (files: BatchRow[], choice: "collections" | "flat") =>
    !!tree && files.length > 0 && (nestable || choice === "collections");
  // A folder of folders sent starts as collections, until the person picks.
  const nestFor = (readable: KnowledgeFormats | null) =>
    plan(true, readable).some((r) => r.folder.includes("/") && r.state !== "skipped") ? "collections" : "flat";
  const [nest, setNest] = useState<"collections" | "flat">(() => nestFor(formats));
  const nestPicked = useRef(false);
  const asCollections = asCollectionsFor(folderFiles, nest);
  // Held to one upload's limits from the first render, before the check answers.
  const [rows, setRows] = useState<BatchRow[]>(() => capBatch(plan(asCollections, formats), false));
  // "partial": too many documents to read them all; "failed": why is in `checkError`.
  const [checked, setChecked] = useState<"checking" | "ok" | "partial" | "failed">("checking");
  const [checkError, setCheckError] = useState("");
  // The collections the check could not read whole.
  const [cut, setCut] = useState<string[]>([]);
  // The same, by name, of the collections an upload read that the check had not.
  const [lateCut, setLateCut] = useState<string[]>([]);
  const [checks, setChecks] = useState(0);
  // Set by the first Upload: a run that only skipped files has started all the same.
  const [started, setStarted] = useState(false);
  const [choice, setChoice] = useState<Choice>("replace");
  // The person's own Replace or Skip, which a later check does not undo.
  const chosen = useRef<Choice | null>(null);
  const [page, setPage] = useState(1);
  const [tagsInput, setTagsInput] = useState(tags);
  const [allowPartial, setAllowPartial] = useState(partial);
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [status, setStatus] = useState("");
  const ctrl = useRef<AbortController | null>(null);
  // The collections this dialog's uploads made: a later upload finds them, and the Library's
  // read after closing waits for the first to be listed.
  const madeHere = useRef<string[]>([]);
  // A folder upload ran: closing has the Library read its collections again, whatever it made.
  const foldersSent = useRef(false);
  // What the check read of each collection, whole or cut at its limit, for the upload to start from.
  const checkedSources = useRef(new Map<string, Map<string, KeptFields>>());
  const leave = () => {
    if (foldersSent.current) {
      const first = madeHere.current[0];
      invalidateSharedCollections(first ? { name: first, org } : undefined);
    }
    onClose();
  };
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

  const { root, treeRows, plans, byFolder, planOf, repeated, topPlans, folderPlace, cutNames, wentTo, deep } =
    useFolderPlan(tree, folderFiles, nestable, asCollections);

  // What is already there: those files are replaced or skipped. Past MAX_CHECK_PAGES the
  // rest is not looked for, and a file whose source exists is skipped (a new source
  // publishes only while none is live). The upload waits for a check that succeeds. A
  // folder's collection that is new holds nothing yet; one it already is is read.
  // The place itself only when some file goes there (a loose one, or one kept by its path).
  const toPlace = rows.some((r) => r.state !== "skipped" && !r.folder);
  // A folder that holds only folders gets no file: its collection is not read.
  const targets = [
    ...(toPlace ? [namespace] : []),
    ...plans.filter((p) => p.existing && p.files > 0).map((p) => p.existing),
  ];
  const targetKey = [...new Set(targets)].sort().join(",");
  // The place itself, or the collections the folders' files go to.
  const checkedWhere = targetKey === namespace ? `in ${place}` : "there";
  const where = (r: BatchRow) => (r.folder ? (planOf(r.folder)?.existing ?? "") : namespace);
  // The formats may arrive after the pick: what is left out is decided again then, while
  // nothing has been sent, each file keeping what the check found of it. A switch between
  // collections and paths changes where each file goes, and so can a folder left out (its
  // collection may now be a sibling's): those check again.
  const replanFor = (readable: KnowledgeFormats | null) => {
    const nowNest = nestPicked.current ? nest : nestFor(readable);
    const nowFolderFiles = foldersIn(readable);
    const mode = asCollectionsFor(nowFolderFiles, nowNest);
    const switched = mode !== asCollections;
    const folderSet = (files: BatchRow[]) => [...new Set(files.map((r) => r.folder))].sort().join("\0");
    const recheck = switched || (mode && folderSet(nowFolderFiles) !== folderSet(folderFiles));
    // By row: in a folder upload two folders' files may share a source URI.
    const found = new Map(rows.map((r) => [r.id, r]));
    const next = plan(mode, readable).map((r) => {
      const was = found.get(r.id);
      return was && !switched ? { ...r, replaces: was.replaces, kept: was.kept } : r;
    });
    // Twins by this render's plan: a re-plan that moves a folder checks again, and the
    // check's answer decides the choice anew.
    const choice = chosen.current ?? firstChoice(next, twinIds(next, where));
    return { nest: nowNest, rows: capBatch(next, choice === "skip"), choice, recheck };
  };
  if (formats !== plannedWith && !started) {
    const next = replanFor(formats);
    setPlannedWith(formats);
    if (next.nest !== nest) setNest(next.nest);
    if (next.recheck) setChecks((n) => n + 1);
    setChoice(next.choice);
    setRows(next.rows);
  }
  // Each change of `checks` (Check again) or of the collections to read reads again; never
  // once files are sent.
  const startedRef = useRef(started);
  startedRef.current = started;
  useEffect(() => {
    if (startedRef.current) return;
    const c = new AbortController();
    setChecked("checking");
    const names = targetKey.split(",").filter(Boolean);
    // Three collections read at a time, each page sent again while the server says it may be.
    inTurn(names, BATCH_OPEN, (ns) =>
      existingSources({ org, namespace: ns }, { signal: c.signal, onPage: setPage, retry: quietRetry(c.signal) }),
    )
      .then((found) => {
        const byNs = new Map(names.map((ns, i) => [ns, found[i]]));
        const complete = found.every((f) => f.complete);
        // Kept for the upload whole or cut: read again, a cut read stops at the same place.
        for (const [ns, f] of byNs) checkedSources.current.set(ns, f.uris);
        setCut(names.filter((ns) => !byNs.get(ns)?.complete));
        const marked = latest.current.map((r) => {
          const uris = byNs.get(where(r))?.uris;
          return { ...r, replaces: !!uris?.has(r.sourceUri), kept: uris?.get(r.sourceUri) };
        });
        const use = chosen.current ?? firstChoice(marked, twinIds(marked, where));
        setChoice(use);
        setRows(capBatch(marked, use === "skip"));
        setChecked(complete ? "ok" : "partial");
        if (refocus.current) requestAnimationFrame(() => uploadRef.current?.focus());
        refocus.current = false;
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        c.abort(); // the other collections' reads stop too
        // A list only reads: nothing can have been saved.
        setCheckError(errorLine(e, { read: true }));
        setChecked("failed");
        if (refocus.current) requestAnimationFrame(() => checkAgainRef.current?.focus());
        refocus.current = false;
      });
    return () => c.abort();
  }, [org, targetKey, checks]);

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
  // `where` reads the plan, which follows the rows' pick.
  const twins = useMemo(() => twinIds(rows, where), [rows, plans]);
  const existing = rows.filter(
    (r) => r.replaces && (r.state === "waiting" || r.state === "next") && !twins.has(r.id),
  ).length;
  const toSend = rows.filter(
    (r) => r.state === "waiting" && !(choice === "skip" && r.replaces) && !twins.has(r.id),
  ).length;
  const failed = rows.filter((r) => r.state === "failed").length;
  const bytes = useMemo(
    () =>
      rows
        .filter((r) => r.state === "waiting" && !(choice === "skip" && r.replaces) && !twins.has(r.id))
        .reduce((n, r) => n + r.file.size, 0),
    [rows, choice, twins],
  );

  // `only`: send again the rows in that state (the failed ones), or, for "next", the ones
  // held past the last upload's limits, capped again.
  const send = async (only?: BatchRow["state"]) => {
    const c = new AbortController();
    ctrl.current = c;
    cancelling.current = false;
    const skipping = choice === "skip";
    let start =
      only === "next"
        ? capBatch(latest.current, skipping)
        : only === "failed"
          ? retryFailed(latest.current, skipping)
          : latest.current;
    setStarted(true);
    setRunning(true);
    requestAnimationFrame(() => statusRef.current?.focus());
    const tags = tagsFrom(tagsInput);
    // What a collection holds, read once per upload (what the check read first); a read that
    // fails fails that collection's files, which Retry failed sends again.
    const reread = new Map<string, Promise<Map<string, KeptFields>>>();
    const readCut = new Set<string>();
    const sourcesOf = (namespace: string) => {
      let read = reread.get(namespace);
      if (!read) {
        const known = checkedSources.current.get(namespace);
        read = known
          ? Promise.resolve(known)
          : existingSources({ org, namespace }, { signal: c.signal, retry: quietRetry(c.signal) }).then((r) => {
              if (!r.complete) readCut.add(namespace);
              return r.uris;
            });
        reread.set(namespace, read);
      }
      return read;
    };
    // The folders' files this upload placed in their collections.
    const placedIds = new Set<number>();
    // Folders: their collections are made first, so each file goes where the server put it.
    if (asCollections && start.some((r) => r.state === "waiting" && r.folder)) {
      foldersSent.current = true;
      setRows(start.map((r) => (r.state === "waiting" && r.folder ? { ...r, line: MAKING } : r)));
      const folders = new FolderCollections(org, byFolder, root, quietRetry(c.signal), madeHere.current);
      const placed = await placeFolders(start, folders, sourcesOf, wentTo, c.signal);
      madeHere.current.push(...folders.created);
      // Collections were made or titled: other views read again, waiting for the first made.
      const made = folders.created[0];
      invalidateSharedCollections(made ? { name: made, org } : undefined);
      const cutHere = new Set<string>();
      for (const p of placed) {
        if (!p.placed) continue;
        placedIds.add(p.row.id);
        if (readCut.has(p.row.target.namespace)) cutHere.add(p.where);
      }
      if (cutHere.size) setLateCut((was) => [...new Set([...was, ...cutHere])]);
      const sending = new Set(start.filter((r) => r.state === "waiting").map((r) => r.id));
      start = afterPlacing(
        placed.map((p) => (p.placed ? { ...p.row, place: p.where } : p.row)),
        placedIds,
        sending,
        where,
        {
          asked: chosen.current !== null,
          skip: skipping,
        },
      );
    }
    // Under "Skip them" the files already there end now, so the count of what is sent holds
    // still while the batch runs.
    const begin = start.map((r) =>
      skipping && r.state === "waiting" && r.replaces ? { ...r, state: "skipped" as const, line: "Already here" } : r,
    );
    setRows(begin);
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
    leave();
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

  const finish = started ? () => void done() : leave;
  useEffect(() => onStarted?.(started), [started, onStarted]);
  useEffect(() => closeWith?.(finish));

  const checking = checked === "checking";
  const checkFailed = checked === "failed";
  const badNames = plans.filter((p) => p.refused);
  const refusedNames = `${badNames
    .slice(0, 3)
    .map((p) => `${p.path} (${p.refused})`)
    .join(", ")}${badNames.length > 3 ? ` and ${badNames.length - 3} more` : ""}`;
  // One plan per collection (a folder that is a sibling's is not one); `making` counts the
  // new ones, not the ones the list says are there already.
  const madePlans = useMemo(() => collectionsMade(plans), [plans]);
  const making = madePlans.filter((p) => !p.existing).length;
  const refused = deep.length > 0 || making > MAX_FOLDERS || badNames.length > 0;
  // Where the button says the files go: the place shown, or, for folders that become new
  // top-level collections, those collections.
  const destination =
    asCollections && !nestable
      ? `${topPlans.length === 1 ? `into ${folderPlace(topPlans[0].path)}` : `into ${topPlans.length} collections`}${toPlace ? ` and ${place}` : ""}`
      : `to ${place}`;
  useEffect(() => onDestination?.(destination), [destination, onDestination]);
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
      {readNote && <Alert state="info" text={readNote} />}
      {folders > 0 && <Alert state="info" text={foldersNotRead(folders, foldersLater)} />}
      {/* Once started, the files go as they were planned, whatever a later read says. */}
      {!started && formatsRead && !formats && (
        <Alert
          state="warning"
          text="Couldn't read which formats this deployment reads: every file is sent, and one it can't read fails on its own."
        />
      )}
      {tree && folderFiles.length > 0 && !nestable && !started && (
        <ChoiceGroup
          label="The folders"
          value={nest}
          onChange={(v) => {
            nestPicked.current = true;
            setNest(v);
            setRows(capBatch(plan(v === "collections", plannedWith), false));
            setChecks((n) => n + 1);
          }}
          options={[
            {
              value: "collections",
              // As New collection says it: people take one to sit in the namespace shown.
              hint: `A top-level collection belongs to the org${tree?.shown ? "" : `, not to ${place}`}: only the members you add read it.`,
              label:
                topPlans.length !== 1
                  ? "Make each dropped folder a top-level collection, its folders inside it"
                  : topPlans[0].existing
                    ? `Add the files to ${folderLabel(topPlans[0], treeRows, repeated)}, its folders inside it`
                    : `Make ${topPlans[0].title} a top-level collection`,
            },
            { value: "flat", label: `Add the files to ${place}, keeping folder paths in their source` },
          ]}
        />
      )}
      {asCollections && plans.length > 0 && (
        <FolderPreview plans={madePlans} place={nestable ? `in ${place}` : ""} label={folderPlace} />
      )}
      {making > MAX_FOLDERS && (
        <Alert
          role="alert"
          state="danger"
          text={`${making} new collections: one pick of folders makes at most ${MAX_FOLDERS}. Pick a folder with fewer folders inside.`}
        />
      )}
      {badNames.length > 0 && (
        <Alert
          role="alert"
          state="danger"
          text={`These folder names can't be collection titles: ${refusedNames}. Rename them, then pick the folder again.`}
        />
      )}
      {deep.length > 0 && (
        <Alert
          role="alert"
          state="danger"
          text={`Collections nest at most ${MAX_DEPTH} deep. These folders are too deep here: ${deep.slice(0, 3).join(", ")}${deep.length > 3 ? ` and ${deep.length - 3} more` : ""}. Pick a folder further down, or upload into a collection nearer the top.`}
        />
      )}
      {checking && (
        <p className="text-xs text-text-muted">{`Checking which files are already ${checkedWhere}${page > 1 && !targetKey.includes(",") ? ` (page ${page})` : ""}…`}</p>
      )}
      {checked === "partial" && (
        <Alert state="warning" text={cutLine(cut.map((ns) => (ns === namespace ? place : (cutNames.get(ns) ?? ns))))} />
      )}
      {lateCut.length > 0 && <Alert state="warning" text={cutLine(lateCut)} />}
      {checkFailed && (
        <div className="space-y-2">
          <Alert state="warning" text={`Couldn't check which files are already ${checkedWhere}: ${checkError}`} />
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
          label={
            targetKey === namespace
              ? `${existing} file${existing === 1 ? " is" : "s are"} already in ${place}`
              : `${existing} file${existing === 1 ? " is" : "s are"} already there`
          }
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
          <FileRow key={r.id} row={r} place={r.place ?? (r.folder ? folderPlace(r.folder) : place)} choice={choice} />
        ))}
      </ul>
      <ModalFooter>
        {!started && (
          <Button type="secondary" htmlType="button" onClick={leave}>
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
                  ? `Checking which files are already ${checkedWhere}…`
                  : checkFailed
                    ? "Check again first."
                    : toSend === 0
                      ? "No file to send: every one is left out or skipped."
                      : refused
                        ? "These folders can't be uploaded as they are: see above."
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
