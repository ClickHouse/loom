/**
 * A file into the knowledge plane: its bytes go to `POST /v1/knowledge.upload`
 * as they are, and `knowledge.ingest` has the server's extractor service parse
 * them and publish the document's next revision. The SDK parses nothing.
 *
 *     const status = await loom.knowledge.ingestFile({
 *       org, namespace, data: bytes, filename: "q3.pdf", source_uri: "repo://q3.pdf",
 *     });
 *     // status.state: ready | partial | failed | cancelled
 */

import type { OpInput, OpOutput } from "./client.js";
import { LoomError, TimeoutError } from "./errors.js";
import type { CallOptions, Transport } from "./transport.js";

/** The states a `knowledge.ingest` job ends in; any other is still running. */
export const INGEST_DONE: ReadonlySet<string> = new Set(["ready", "partial", "failed", "cancelled"]);

/** A file's bytes: a byte array, an `ArrayBuffer`, or a `Blob` (a browser `File`). */
export type FileData = Uint8Array | ArrayBuffer | Blob;

/** What `knowledge.upload` answers: the scoped id `knowledge.ingest` names. */
export interface Upload {
  upload_id: string;
  namespace: string;
  sha256: string;
  size: number;
  filename: string;
  /** The format the file's name (else its media type) gives. */
  format: string;
  expires_at: string;
}

export interface UploadOptions extends CallOptions {
  /** The file's media type; names the format when the file name does not. */
  contentType?: string;
}

export interface WaitOptions {
  /** How long to wait for the job to end, default 600 s. */
  timeoutMs?: number;
  /** Between polls, default 1 s. */
  pollMs?: number;
  signal?: AbortSignal;
}

type IngestInput = OpInput<"knowledge.ingest">;
type IngestStatus = OpOutput<"knowledge.ingest_status">;

/** `ingestFile`'s input: `knowledge.ingest`'s fields with the file in place of `upload_id`. */
export type IngestFileInput = Omit<IngestInput, "upload_id" | "title"> & {
  data: FileData;
  filename: string;
  contentType?: string;
  /** Default: the file name less its extension, as the ingest CLI titles it. */
  title?: string;
  /** Wait for the job to end (default) or return `knowledge.ingest`'s answer. */
  wait?: boolean;
} & WaitOptions;

/** The `loom.knowledge` methods beside the generated ops. */
export interface KnowledgeFiles {
  /** Store a file's bytes for `knowledge.ingest`, sent with their sha256 for the server to check. */
  upload(
    scope: { org: string; namespace: string },
    data: FileData,
    filename: string,
    opts?: UploadOptions,
  ): Promise<Upload>;
  /** `knowledge.ingest_status` once the job ends; a job not found returns at once. Throws `TimeoutError` past `timeoutMs`. */
  waitForIngest(
    scope: { org: string; namespace: string },
    ingestionId: string,
    opts?: WaitOptions,
  ): Promise<IngestStatus>;
  /** Upload a file and ingest it: the job's final status, or with `wait: false` the ingest answer. */
  ingestFile(input: IngestFileInput & { wait: false }): Promise<OpOutput<"knowledge.ingest">>;
  ingestFile(input: IngestFileInput): Promise<IngestStatus>;
}

/** The bytes, in an `ArrayBuffer` as `fetch` and `crypto.subtle` take them (a view of a shared buffer is copied). */
async function bytesOf(data: FileData): Promise<Uint8Array<ArrayBuffer>> {
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (data instanceof Uint8Array)
    return data.buffer instanceof ArrayBuffer ? (data as Uint8Array<ArrayBuffer>) : new Uint8Array(data);
  return new Uint8Array(await data.arrayBuffer());
}

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function stem(filename: string): string {
  const name = filename.replace(/\\/g, "/").split("/").pop() ?? filename;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

const defaultSleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(signal.reason ?? new LoomError("aborted"));
      },
      { once: true },
    );
  });

/** The methods bound to a transport; `LoomClient` merges them into `loom.knowledge`. */
export function knowledgeFiles(
  transport: Transport,
  now: () => number = () => Date.now(),
  sleep: (ms: number, signal?: AbortSignal) => Promise<void> = defaultSleep,
): KnowledgeFiles {
  const upload = async (
    scope: { org: string; namespace: string },
    data: FileData,
    filename: string,
    opts: UploadOptions = {},
  ): Promise<Upload> => {
    const bytes = await bytesOf(data);
    const { contentType = "", ...call } = opts;
    const query = {
      org: scope.org,
      namespace: scope.namespace,
      filename,
      content_type: contentType,
      sha256: await sha256Hex(bytes),
    };
    return transport.postBytes<Upload>("knowledge.upload", query, bytes, call);
  };

  const waitForIngest = async (
    scope: { org: string; namespace: string },
    ingestionId: string,
    opts: WaitOptions = {},
  ): Promise<IngestStatus> => {
    const { timeoutMs = 600_000, pollMs = 1_000, signal } = opts;
    const deadline = now() + timeoutMs;
    for (;;) {
      const status = await transport.post<IngestStatus>(
        "knowledge.ingest_status",
        { org: scope.org, namespace: scope.namespace, ingestion_id: ingestionId },
        { signal },
      );
      if (!status.found || INGEST_DONE.has(status.state ?? "")) return status;
      if (now() >= deadline)
        throw new TimeoutError(
          `ingestion ${ingestionId} is still ${status.state} after ${timeoutMs} ms; knowledge.ingest_status follows it`,
          { op: "knowledge.ingest_status" },
        );
      await sleep(pollMs, signal);
    }
  };

  async function ingestFile(input: IngestFileInput): Promise<IngestStatus | OpOutput<"knowledge.ingest">> {
    const { data, filename, contentType, wait = true, timeoutMs, pollMs, signal, ...fields } = input;
    const up = await upload(fields, data, filename, { contentType, signal });
    const job = await transport.post<OpOutput<"knowledge.ingest">>(
      "knowledge.ingest",
      { ...fields, title: fields.title ?? stem(filename), upload_id: up.upload_id },
      { signal },
    );
    if (!wait) return job;
    return waitForIngest(fields, job.ingestion_id, { timeoutMs, pollMs, signal });
  }

  return { upload, waitForIngest, ingestFile: ingestFile as KnowledgeFiles["ingestFile"] };
}
