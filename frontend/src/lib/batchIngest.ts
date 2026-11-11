/**
 * Many files into the knowledge plane at once, each through the one-file path
 * (`ingestFile`: upload, ingest, follow the job to its end).
 *
 * At most `BATCH_OPEN` of a batch's jobs are open at a time: the ingest worker takes one
 * job per namespace per pass, first in first out, so a batch that queued everything
 * would put every agent writing there behind it. A step the server says may be sent again
 * (`retryable` in the error body, a rate limit among them) is, after the wait it asks for;
 * anything else fails that file alone, and the others go on.
 */
import {
  cancelIngestion,
  type Identity,
  type KnowledgeFormats,
  type KnowledgeIngestStatus,
  listKnowledge,
} from "../api";
import { ApiError } from "./errors";
import { errorLine, humanizeError } from "./errorWords";
import { FollowLost, fileVerdict, ingestClock, ingestFile, titleFromName } from "./fileIngest";

/** A batch's jobs open at once (queued or running), across all its namespaces. */
export const BATCH_OPEN = 3;
/** The most files one batch takes. */
export const MAX_BATCH_FILES = 200;
/** The most bytes one batch takes, all files together. */
export const MAX_BATCH_BYTES = 1024 * 1024 * 1024;
/** How long one step is sent again while the server asks to wait. */
const RETRY_FOR_MS = 60_000;
/** The line of a row not sent yet. */
export const WAITING = "Not sent yet";

/** What operating systems leave that is not hidden by its dot (`.DS_Store` is). */
const JUNK_NAMES = new Set(["thumbs.db", "desktop.ini"]);

export type RowState =
  | "waiting" // not sent yet
  | "sending" // its bytes, or its ingest call, on their way
  | "working" // its job queued or running on the server
  | "done"
  | "partial"
  | "failed"
  | "skipped"
  | "not_sent" // stopped: never sent, or its job cancelled
  | "next" // past one upload's limits: Upload the next files sends it
  | "goes_on"; // queued, then no longer heard of, or stopped while it may still publish: it goes on on the server

/** One file of a batch and where it is. */
export interface BatchRow {
  /** Unique in the batch: its place in the pick. */
  id: number;
  /** What it is shown as: the path it was picked or dropped with. */
  key: string;
  file: File;
  /** Where it goes. */
  target: Pick<Identity, "org" | "namespace">;
  /** The source it publishes as. The same as `key` here; a file that a picked folder files
   *  into a collection keeps its path as `key` and goes by its own name. */
  sourceUri: string;
  title: string;
  state: RowState;
  /** The line shown under its state: its step, what it came to, or why it was skipped. */
  line: string;
  /** A document with its source already in the target, which this file replaces. */
  replaces?: boolean;
  /** That document's title, description and tags, which a replacement keeps. */
  kept?: KeptFields;
  /** Its job, while open, so Stop can cancel it. */
  ingestionId?: string;
}

/** What a replaced document keeps, as the one-file Replace does. */
export interface KeptFields {
  title: string;
  description: string;
  tags: string[];
}

/** A file and the path it came with ("" for a file picked alone). */
export interface PickedEntry {
  file: File;
  path: string;
}

/** What a drop left unread, in words; "" for none. */
export function foldersNotRead(folders: number): string {
  if (!folders) return "";
  return `${folders} folder${folders === 1 ? " was" : "s were"} not read: drop or pick the files inside ${folders === 1 ? "it" : "them"}.`;
}

/** `rows` held to one upload's limits, counted over the files it will send (with `skip`,
 *  the files already there are not counted): past the 200th file in path order or the
 *  1 GB mark, a file waits for Upload the next files (state `next`). */
export function capBatch(rows: BatchRow[], skip: boolean): BatchRow[] {
  const fresh = rows.map((r) => (r.state === "next" ? { ...r, state: "waiting" as const, line: WAITING } : r));
  const order = fresh
    .map((r, i) => [r, i] as const)
    .filter(([r]) => r.state === "waiting" && !(skip && r.replaces))
    .sort(([a], [b]) => a.key.localeCompare(b.key));
  // A prefix in path order: from the first file past a limit, every later one waits. One
  // file always goes, however large (the server's own limit refuses it, if any).
  const over = new Set<number>();
  let count = 0;
  let bytes = 0;
  for (const [r, i] of order) {
    const past = count > 0 && (count + 1 > MAX_BATCH_FILES || bytes + r.file.size > MAX_BATCH_BYTES);
    if (past || over.size > 0) over.add(i);
    else {
      count++;
      bytes += r.file.size;
    }
  }
  return fresh.map((r, i) =>
    over.has(i) ? { ...r, state: "next" as const, line: "Sent by Upload the next files" } : r,
  );
}

/** Why a file is left out of a batch before anything is sent, or "". Unlike the one-file
 *  form, a file whose name and type match no listed format is not read as the default
 *  one: in a batch it is most likely a picture, an archive or a program. */
export function skipReason(entry: PickedEntry, formats: KnowledgeFormats | null): string {
  const segments = (entry.path || entry.file.name).split("/");
  const name = segments[segments.length - 1];
  if (segments.some((s) => s.startsWith(".")) || segments.includes("__MACOSX")) return "Hidden";
  if (JUNK_NAMES.has(name.toLowerCase()) || name.startsWith("~$")) return "Left by the operating system or an editor";
  const verdict = fileVerdict(entry.file, formats);
  if (verdict.refusal) return verdict.refusal;
  if (formats && !verdict.listed) return "Not a format this deployment reads";
  return "";
}

/** The failed rows sent again, held to one upload's limits: those past them wait for Upload
 *  the next files, and the files already waiting for it stay there. */
export function retryFailed(rows: BatchRow[], skip: boolean): BatchRow[] {
  // Only the failed ones are counted: the held ones are kept out of the cap, then put back.
  const held = new Map(rows.filter((r) => r.state === "next").map((r) => [r.id, r]));
  const reset = rows.map((r) =>
    r.state === "failed"
      ? { ...r, state: "waiting" as const, line: WAITING }
      : held.has(r.id)
        ? { ...r, state: "not_sent" as const }
        : r,
  );
  return capBatch(reset, skip).map((r) => held.get(r.id) ?? r);
}

/** The rows a batch starts with: one per file, the ones left out already skipped. */
export function planBatch(
  entries: PickedEntry[],
  formats: KnowledgeFormats | null,
  target: (entry: PickedEntry) => Pick<Identity, "org" | "namespace">,
): BatchRow[] {
  const seen = new Set<string>();
  return entries.map((entry, id) => {
    const key = entry.path || entry.file.name;
    const where = target(entry);
    const sourceUri = key;
    const title = titleFromName(entry.file.name);
    const row: BatchRow = { id, key, file: entry.file, target: where, sourceUri, title, state: "waiting", line: "" };
    const why = skipReason(entry, formats);
    const place = `${where.namespace}\0${sourceUri}`;
    if (why) return { ...row, state: "skipped", line: why };
    if (seen.has(place))
      return {
        ...row,
        state: "skipped",
        line: entry.path ? "Same path as another file here" : "Same name as another file here",
      };
    seen.add(place);
    return { ...row, line: WAITING };
  });
}

/** Pages of `knowledge.list` read to find what is already there, and their size. */
export const MAX_CHECK_PAGES = 20;
const CHECK_PAGE = 500;
/** The most documents of a place that check reads. */
export const MAX_CHECKED = MAX_CHECK_PAGES * CHECK_PAGE;

/** The documents already live in `id`, by source URI, with what a replacement keeps, and
 *  whether that is all of them: past `MAX_CHECK_PAGES` pages the read stops. */
export async function existingSources(
  id: Pick<Identity, "org" | "namespace">,
  opts: { signal?: AbortSignal; onPage?: (page: number) => void } = {},
): Promise<{ uris: Map<string, KeptFields>; complete: boolean }> {
  const uris = new Map<string, KeptFields>();
  let cursor: string | undefined;
  for (let page = 1; page <= MAX_CHECK_PAGES; page++) {
    opts.onPage?.(page);
    const r = await listKnowledge(id, {
      fields: ["source_uri", "title", "description", "tags"],
      limit: CHECK_PAGE,
      cursor,
      signal: opts.signal,
    });
    for (const row of r.rows)
      uris.set(row.source_uri, { title: row.title ?? "", description: row.description ?? "", tags: row.tags ?? [] });
    if (!r.next_cursor) return { uris, complete: true };
    cursor = r.next_cursor;
  }
  return { uris, complete: false };
}

/** Whether the server said the request may be sent again unchanged, and after how long. */
function retryAfterMs(e: unknown): number | null {
  if (!(e instanceof ApiError) || e.detail["retryable"] !== true) return null;
  const s = Number(e.detail["retry_after_s"]);
  return Number.isFinite(s) && s > 0 ? s * 1000 : 1000;
}

/** `step`, sent again while the server says it may be, for at most a minute. */
export async function withRetry<T>(
  step: () => Promise<T>,
  onWait: (line: string) => void,
  signal?: AbortSignal,
): Promise<T> {
  const until = Date.now() + RETRY_FOR_MS;
  for (let backoff = 1000; ; backoff = Math.min(backoff * 2, 8000)) {
    try {
      return await step();
    } catch (e) {
      const wait = retryAfterMs(e);
      const pause = wait === null ? 0 : Math.max(wait, backoff);
      if (wait === null || signal?.aborted || Date.now() + pause > until) throw e;
      onWait(e instanceof ApiError && e.code === "RATE_LIMITED" ? "Waiting (rate limit)…" : "Waiting (server busy)…");
      await ingestClock.sleep(pause, signal);
      signal?.throwIfAborted(); // stopped while it waited: not sent again
    }
  }
}

export interface BatchOptions {
  fields: { tags: string[]; allow_partial: boolean };
  /** Sends no more files. A job already queued goes on, unless `cancelling` says the
   *  person stopped the batch, when its row cancels it. */
  signal: AbortSignal;
  cancelling?: () => boolean;
  onRow: (row: BatchRow) => void;
}

/** Send every waiting row, `BATCH_OPEN` at a time, reporting each change with `onRow`. */
export async function runBatch(rows: BatchRow[], opts: BatchOptions): Promise<void> {
  const queue = rows.filter((r) => r.state === "waiting");
  const worker = async () => {
    for (let row = queue.shift(); row; row = queue.shift()) {
      if (opts.signal.aborted) {
        opts.onRow({ ...row, state: "not_sent", line: "" });
        continue;
      }
      await sendRow(row, opts);
    }
  };
  await Promise.all(Array.from({ length: BATCH_OPEN }, worker));
}

async function sendRow(start: BatchRow, opts: BatchOptions): Promise<void> {
  let row = start;
  // Whether its bytes are still on their way: until they land, nothing can be queued.
  let uploading = true;
  const set = (patch: Partial<BatchRow>) => {
    row = { ...row, ...patch };
    opts.onRow(row);
  };
  // A row sent again (Retry failed) may carry the job of its last run: that one is over.
  set({ state: "sending", line: "Uploading…", ingestionId: undefined });
  try {
    const status: KnowledgeIngestStatus = await ingestFile(
      row.target,
      row.file,
      {
        source_uri: row.sourceUri,
        // A replacement keeps the document's title, description and tags, as the one-file
        // Replace does; the batch's tags are added to them.
        title: row.kept?.title || row.title,
        description: row.kept?.description ?? "",
        tags: [...new Set([...(row.kept?.tags ?? []), ...opts.fields.tags])],
        allow_partial: opts.fields.allow_partial,
        // A new source publishes only while none is live: a second job for it (a retry
        // after a lost answer) fails instead of publishing twice. A replacement publishes
        // over whatever is live, as the one-file Replace does.
        ...(row.replaces ? {} : { expected_revision: 0 }),
      },
      (line, phase) => {
        uploading = phase === "upload";
        set({ state: phase === "follow" ? "working" : "sending", line });
      },
      {
        signal: opts.signal,
        onQueued: (ingestionId) => set({ ingestionId }),
        retry: (step) => withRetry(step, (line) => set({ line }), opts.signal),
      },
    );
    set({ ingestionId: undefined, ...ended(status, row) });
  } catch (e) {
    if (opts.signal.aborted && row.ingestionId) {
      if (opts.cancelling?.()) await cancelRow(row.target, row.ingestionId, set);
      else set({ state: "goes_on", line: STILL });
      return;
    }
    // Stopped before its ingest call, while waiting to send it again, or after the server
    // refused it as one to send again: nothing was queued. An ingest call that failed
    // otherwise after Stop may have queued it, and its line says so.
    const unqueued =
      uploading ||
      (e as { name?: unknown }).name === "AbortError" ||
      (e instanceof ApiError && e.detail["retryable"] === true);
    if (opts.signal.aborted && unqueued) {
      set({ state: "not_sent", line: "" });
      return;
    }
    if (e instanceof FollowLost) {
      set({ state: "goes_on", line: STILL });
      return;
    }
    // A failed ingest call says itself whether it may have queued the job; a failed upload
    // leaves nothing in the list to check, so it is worded as a read.
    set({ state: "failed", line: humanizeError(e, { read: uploading }) });
  }
}

const STILL = "Loom stopped reporting on it; it appears once it publishes and the list is reloaded";

/** Cancel a stopped row's job (`knowledge.ingest_cancel`): one that published meanwhile
 *  is added, one that ended otherwise says how. Every open job is cancelled by its own
 *  row, also one whose ingest answered after Stop. */
async function cancelRow(
  target: BatchRow["target"],
  ingestionId: string,
  set: (patch: Partial<BatchRow>) => void,
): Promise<void> {
  set({ line: "Stopping…" });
  try {
    const out = await cancelIngestion(target, ingestionId);
    if (out.found && (out.state === "ready" || out.state === "partial"))
      set({ state: "done", line: "Added before it could stop" });
    // A running job stops before it publishes, unless it was already publishing.
    else if (out.cancelled && out.state === "processing")
      set({ state: "goes_on", line: "Stop asked; if it was already publishing, it appears once the list is reloaded" });
    else if (out.state === "failed") set({ state: "failed", line: "It had failed" });
    else set({ state: "not_sent", line: "Stopped" });
  } catch (e) {
    set({ state: "goes_on", line: `Stop failed, so it may go on: ${errorLine(e)}` });
  }
}

/** A finished job's row state and line. */
function ended(status: KnowledgeIngestStatus, row: BatchRow): Partial<BatchRow> {
  if (!status.found) return { state: "failed", line: "Loom no longer knows this job" };
  if (status.state === "ready") return { state: "done", line: `Revision ${status.revision}` };
  if (status.state === "partial") {
    const lost = status.coverage?.lost?.length ? status.coverage.lost.join(", ") : "part of it";
    return { state: "partial", line: `Not read: ${lost}` };
  }
  if (status.state === "cancelled") return { state: "not_sent", line: "Stopped" };
  if (status.error_code === "revision_conflict" && !row.replaces)
    return { state: "skipped", line: "A document with this source is already here" };
  return { state: "failed", line: status.error_message || "The file could not be read" };
}
