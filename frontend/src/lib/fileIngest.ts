/**
 * A file into the knowledge plane from the dashboard: its bytes are uploaded as they are
 * and `knowledge.ingest` has the server's extractor parse them; the browser reads
 * nothing of the file. Shared by the Library's add dialog and a repository's.
 */

import {
  ingestKnowledge,
  type Identity,
  type KnowledgeFormat,
  type KnowledgeFormats,
  type KnowledgeIngestFields,
  type KnowledgeIngestStatus,
  knowledgeIngestStatus,
  uploadKnowledgeFile,
} from "../api";

/** The states an ingest job ends in. */
const INGEST_DONE = new Set(["ready", "partial", "failed", "cancelled"]);

/** What the deployment will do with a picked file, before it is sent. */
export interface FileVerdict {
  /** The format it is read as; "" when it is refused or the server did not list its formats. */
  format: string;
  /** False when no configured extractor reads that format; null when the server did not say. */
  available: boolean | null;
  /** Why it will not be sent, in words a person can act on; "" when it can be. */
  refusal: string;
}

/** The format the server reads `file` as (its extension, else its media type, else the
 *  default), as `knowledge.upload` names it, and whether it can be sent at all. */
export function fileVerdict(
  file: { name: string; type: string; size: number },
  formats: KnowledgeFormats | null,
): FileVerdict {
  const ext = file.name.includes(".") ? (file.name.split(".").pop() ?? "").toLowerCase() : "";
  if (!formats) return { format: "", available: null, refusal: "" };
  if (formats.refused_extensions.includes(ext))
    return { format: "", available: false, refusal: `.${ext} files are not read; save it as .${ext}x and pick that.` };
  // The upload refuses an empty body too.
  if (file.size === 0) return { format: "", available: false, refusal: `${file.name} is empty.` };
  if (file.size > formats.max_upload_bytes)
    return {
      format: "",
      available: false,
      refusal: `${file.name} is ${mb(file.size)}; this deployment takes files up to ${mb(formats.max_upload_bytes)}.`,
    };
  const media = file.type.split(";")[0].trim().toLowerCase();
  const entry: KnowledgeFormat | undefined =
    formats.formats.find((f) => f.extensions.includes(ext)) ??
    formats.formats.find((f) => media !== "" && f.media_types.includes(media)) ??
    formats.formats.find((f) => f.format === formats.default_format);
  if (!entry) return { format: "", available: null, refusal: "" }; // a listing without its default format
  // Unknown (null) is no refusal: the extractor could not be asked just now, and an
  // ingest sent waits for it.
  return {
    format: entry.format,
    available: entry.available,
    refusal: entry.available === false ? `No extractor on this deployment reads ${entry.format} files.` : "",
  };
}

function mb(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** One line on where a job is, for a person watching it. */
export function ingestStep(
  status: Pick<KnowledgeIngestStatus, "state" | "stage" | "revision" | "chunks" | "error_code" | "error_message">,
): string {
  switch (status.state) {
    case "pending":
      return status.error_code === "retrying" ? "Waiting to try again…" : "Queued…";
    case "processing":
      return status.stage === "embed" ? "Embedding…" : status.stage === "publish" ? "Publishing…" : "Extracting…";
    case "ready":
      return `Ready: revision ${status.revision}, ${chunks(status.chunks)}`;
    case "partial":
      return `Published part of it: revision ${status.revision}, ${chunks(status.chunks)}`;
    case "cancelled":
      return "Cancelled";
    case "failed":
      return `Failed${status.error_code ? ` (${status.error_code})` : ""}: ${status.error_message || "the extraction did not publish"}`;
    default:
      return "No such job";
  }
}

function chunks(n: number): string {
  return `${n} chunk${n === 1 ? "" : "s"}`;
}

// Between two polls of a job.
const POLL_MS = 1000;
// Polls in a row that may fail before the page stops following a queued job.
const POLL_MISSES = 3;

/** A queued job the page could no longer follow: it goes on on the server, so a dialog
 *  says so rather than offering to send the file again. */
export class FollowLost extends Error {
  constructor(
    readonly ingestionId: string,
    cause: unknown,
  ) {
    super(`lost track of ingestion ${ingestionId}`, { cause });
    this.name = "FollowLost";
  }
}

/** The pause between polls; tests replace it to follow a job without waiting. */
export const ingestClock = {
  sleep: (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms)),
};

/** Where a file is: its bytes on their way (cancellable: nothing is queued yet), the
 *  ingest call (held: it may queue the job), or its job being followed. */
export type IngestPhase = "upload" | "queue" | "follow";

/**
 * Upload `file`, ingest it and follow the job until it ends, calling `onStep` with each
 * line to show and the phase it is in. Answers the job's last status. `signal` stops
 * the upload (which it alone bounds, so a large file is not cut off) and the following;
 * each poll keeps the default request bound. The job itself goes on on the server.
 */
export async function ingestFile(
  id: Pick<Identity, "org" | "namespace">,
  file: File,
  fields: KnowledgeIngestFields,
  onStep: (line: string, phase: IngestPhase) => void,
  opts: { signal?: AbortSignal } = {},
): Promise<KnowledgeIngestStatus> {
  const { signal } = opts;
  onStep("Uploading…", "upload");
  const up = await uploadKnowledgeFile(id, file, signal);
  signal?.throwIfAborted(); // cancelled while the bytes went: nothing is queued
  onStep("Queuing…", "queue");
  const job = await ingestKnowledge(id, { ...fields, upload_id: up.upload_id });
  let missed = 0;
  for (;;) {
    signal?.throwIfAborted();
    let status: KnowledgeIngestStatus;
    try {
      status = await knowledgeIngestStatus(id, job.ingestion_id);
      missed = 0;
    } catch (e) {
      // A poll that fails (a replica restarting, a rate limit, a dropped connection) is
      // tried again; the job is queued and goes on whatever the page hears of it.
      if (++missed >= POLL_MISSES) throw new FollowLost(job.ingestion_id, e);
      await ingestClock.sleep(POLL_MS);
      continue;
    }
    signal?.throwIfAborted();
    onStep(ingestStep(status), "follow");
    if (!status.found || INGEST_DONE.has(status.state)) return status;
    await ingestClock.sleep(POLL_MS);
  }
}
