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
  type KnowledgeIngestStatus,
  knowledgeIngestStatus,
  uploadKnowledgeFile,
} from "../api";

/** The states an ingest job ends in. */
export const INGEST_DONE = new Set(["ready", "partial", "failed", "cancelled"]);

/** What the deployment will do with a picked file, before it is sent. */
export interface FileVerdict {
  /** The format it is read as; "" when the name is refused. */
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
  if (!entry) return { format: formats.default_format, available: null, refusal: "" };
  return {
    format: entry.format,
    available: entry.available,
    refusal: entry.available ? "" : `No extractor on this deployment reads ${entry.format} files.`,
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
      return `Ready: revision ${status.revision}, ${status.chunks} chunks`;
    case "partial":
      return `Published part of it: revision ${status.revision}, ${status.chunks} chunks`;
    case "cancelled":
      return "Cancelled";
    case "failed":
      return `Failed${status.error_code ? ` (${status.error_code})` : ""}: ${status.error_message || "the extraction did not publish"}`;
    default:
      return "No such job";
  }
}

export interface IngestFields {
  source_uri: string;
  title?: string;
  description?: string;
  tags?: string[];
  allow_partial?: boolean;
}

/**
 * Upload `file`, ingest it and follow the job until it ends, calling `onStep` with each
 * line to show. Answers the job's last status. `signal` stops the upload and the polls;
 * the job itself goes on on the server.
 */
export async function ingestFile(
  id: Pick<Identity, "org" | "namespace">,
  file: File,
  fields: IngestFields,
  onStep: (line: string, status?: KnowledgeIngestStatus) => void,
  opts: { signal?: AbortSignal; pollMs?: number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<KnowledgeIngestStatus> {
  const { signal, pollMs = 1000, sleep = (ms) => new Promise((r) => setTimeout(r, ms)) } = opts;
  onStep("Uploading…");
  const up = await uploadKnowledgeFile(id, file, signal);
  const job = await ingestKnowledge(id, { ...fields, upload_id: up.upload_id });
  for (;;) {
    const status = await knowledgeIngestStatus(id, job.ingestion_id, { signal });
    onStep(ingestStep(status), status);
    if (!status.found || INGEST_DONE.has(status.state)) return status;
    await sleep(pollMs);
  }
}
