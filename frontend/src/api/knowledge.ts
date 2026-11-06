// The API client is split by plane behind the `api.ts` barrel, so a view's client rides
// in that view's chunk and the shell evaluates only the functions it calls. Importers
// still import from `api.ts`.
import { getResponse, post, postFile } from "../api";
import type { Identity, RequestOpts } from "../api";
import { ApiError } from "../lib/errors";
import type { Locator } from "../lib/locator";

// ===== Knowledge =====

export interface KnowledgeEntry {
  source_uri: string;
  title: string;
  /** User-supplied summary. Rendered as a 2-line excerpt in the Library
   *  list, and stored in each chunk's context (embedded and lexically scored). */
  description?: string;
  tags: string[];
  ingested_at: string;
  chunk_count: number;
  superseded: boolean;
  /** True when the original uploaded file was preserved and is
   *  downloadable. False for plain-text uploads. */
  has_blob?: boolean;
  blob_size?: number;
  blob_content_type?: string;
  original_filename?: string;
  /** First ~200 chars of extracted text — UI snippet. */
  content_preview?: string;
}

export interface KnowledgeListResponse {
  rows: KnowledgeEntry[];
  next_cursor: string | null;
}

export async function listKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  opts: {
    tags?: string[];
    include_superseded?: boolean;
    limit?: number;
    cursor?: string;
    fields?: string[];
    /** Only documents whose source_uri starts with one of these. */
    uriPrefix?: string[];
  } & RequestOpts = {},
): Promise<KnowledgeListResponse> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    tags: opts.tags || [],
    // Two or more must all match (a build that predates `tags_every` matches any one).
    ...((opts.tags?.length ?? 0) > 1 ? { tags_every: true } : {}),
    include_superseded: opts.include_superseded || false,
    limit: opts.limit || 100,
    ...(opts.uriPrefix?.length ? { uri_prefix: opts.uriPrefix } : {}),
    // The fields the caller reads; unset, every field.
    ...(opts.fields ? { fields: opts.fields } : {}),
  };
  if (opts.cursor) body["cursor"] = opts.cursor;
  return post<KnowledgeListResponse>("/v1/knowledge.list", body, opts.signal);
}

/** Edit doc-level metadata (title / description / tags) without
 *  re-chunking or re-embedding the body unless `reindex` is set. Without
 *  it, cheap (single doc-row insert). Returns the post-update state so the
 *  caller doesn't need to refetch. */
export async function updateKnowledgeMetadata(
  id: Pick<Identity, "org" | "namespace">,
  source_uri: string,
  patch: {
    title?: string;
    description?: string;
    tags?: string[];
    /** When true, rewrite every chunk with the new title + description in
     *  its context (re-embedded and re-scored by the lexical leg), as a full
     *  content re-upload would, without re-uploading the file. */
    reindex?: boolean;
  },
): Promise<{ updated: boolean; title: string; description: string; tags: string[]; chunks_reindexed: number }> {
  return post("/v1/knowledge.update_metadata", {
    org: id.org,
    namespace: id.namespace,
    source_uri,
    ...(patch.title !== undefined ? { title: patch.title } : {}),
    ...(patch.description !== undefined ? { description: patch.description } : {}),
    ...(patch.tags !== undefined ? { tags: patch.tags } : {}),
    ...(patch.reindex ? { reindex: true } : {}),
  });
}

export async function deleteKnowledge(id: Pick<Identity, "org" | "namespace">, sourceUri: string): Promise<void> {
  await post<void>("/v1/knowledge.delete", {
    org: id.org,
    namespace: id.namespace,
    source_uri: sourceUri,
  });
}

export interface KnowledgeBulkDeleteResult {
  source_uri: string;
  tombstoned: boolean;
  chunks_tombstoned: number;
  reason: string;
}

export async function bulkDeleteKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  sourceUris: string[],
): Promise<{ results: KnowledgeBulkDeleteResult[]; total_tombstoned: number }> {
  return post("/v1/knowledge.bulk_delete", {
    org: id.org,
    namespace: id.namespace,
    source_uris: sourceUris,
  });
}

export interface KnowledgeDocument {
  found: boolean;
  source_uri: string;
  title?: string | null;
  content?: string | null;
  tags?: string[] | null;
  ingested_at?: string | null;
  superseded?: boolean | null;
  doc_id?: string | null;
}

export async function getKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  sourceUri: string,
): Promise<KnowledgeDocument> {
  return post<KnowledgeDocument>("/v1/knowledge.get", {
    org: id.org,
    namespace: id.namespace,
    source_uri: sourceUri,
  });
}

/** How often agents opened each document over `days` (`knowledge.get` reads; the dashboard's own previews left out). */
export interface KnowledgeReadRow {
  source_uri: string;
  reads: number;
  agents: number;
  last_read_at: string;
}
export async function knowledgeReads(
  id: Pick<Identity, "org" | "namespace">,
  opts: { days?: number; source_uris?: string[] } & RequestOpts = {},
): Promise<{ window_days: number; reads: KnowledgeReadRow[] }> {
  return post(
    "/v1/knowledge.reads",
    {
      org: id.org,
      namespace: id.namespace,
      days: opts.days ?? 7,
      source_uris: opts.source_uris ?? [],
    },
    opts.signal,
  );
}

export interface KnowledgeAddResponse {
  doc_id: string;
  chunks_created: number;
  embedding_model: string;
  embedding_dim: number;
  /** Surfaced by knowledge.upsert: the source_uri of an existing
   *  document with the same content hash, so the UI can warn about
   *  cross-URI duplication. Empty string when the upload is fresh. */
  duplicate_of_uri?: string;
  duplicate_chunk_count?: number;
  chunks_superseded?: number;
}

export async function addKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  opts: {
    source_uri: string;
    content: string;
    title?: string;
    description?: string;
    tags?: string[];
    upsert?: boolean;
  },
): Promise<KnowledgeAddResponse> {
  const body: Record<string, unknown> = {
    org: id.org,
    namespace: id.namespace,
    source_uri: opts.source_uri,
    content: opts.content,
    title: opts.title || "",
    description: opts.description || "",
    tags: opts.tags || [],
  };
  // knowledge.upsert replaces existing chunks for the same source_uri;
  // knowledge.add is add-only and 409s on duplicates. Default to upsert.
  const path = opts.upsert === false ? "/v1/knowledge.add" : "/v1/knowledge.upsert";
  return post<KnowledgeAddResponse>(path, body);
}

/** A passage `knowledge.search` found: its document, where it sits (`locator`, read for
 *  its `format`), the revision it was read from, and the text. */
export interface KnowledgeHit {
  source_uri: string;
  chunk_idx: number;
  content: string;
  section: string;
  score: number;
  // A server older than revisions and locators sends none of the fields below.
  revision?: number;
  format?: string;
  block_type?: string;
  locator?: Locator | null;
  keys_matched?: string[];
}

/** The namespace's best `topK` passages that answer `query`, best first (`knowledge.search`). */
export async function searchKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  query: string,
  topK: number,
  opts: RequestOpts = {},
): Promise<KnowledgeHit[]> {
  const r = await post<{ results: KnowledgeHit[] }>(
    "/v1/knowledge.search",
    { org: id.org, namespace: id.namespace, query, top_k: topK, namespace_scope: "this" },
    opts.signal,
  );
  return r.results;
}

/** One format `knowledge.formats` lists: the names that select it, and whether a
 *  configured extractor reads it (null while the server could not ask it). */
export interface KnowledgeFormat {
  format: string;
  extensions: string[];
  media_types: string[];
  available: boolean | null;
}

export interface KnowledgeFormats {
  formats: KnowledgeFormat[];
  max_upload_bytes: number;
  refused_extensions: string[];
  /** The format of a file whose name and type select none. */
  default_format: string;
}

/** The formats this deployment's extractors read; null from a server that predates the
 *  op (a 404), whose answer the page then does not guess. */
export async function knowledgeFormats(
  id: Pick<Identity, "org" | "namespace">,
  opts: RequestOpts = {},
): Promise<KnowledgeFormats | null> {
  try {
    return await post<KnowledgeFormats>("/v1/knowledge.formats", { org: id.org, namespace: id.namespace }, opts.signal);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export interface KnowledgeUpload {
  upload_id: string;
  sha256: string;
  size: number;
  filename: string;
  format: string;
  expires_at: string;
}

/** Store a file's bytes for `knowledge.ingest`; the server parses nothing here. */
export async function uploadKnowledgeFile(
  id: Pick<Identity, "org" | "namespace">,
  file: File,
  signal?: AbortSignal,
): Promise<KnowledgeUpload> {
  const q = new URLSearchParams({
    org: id.org,
    namespace: id.namespace,
    filename: file.name,
    content_type: file.type,
  });
  return postFile<KnowledgeUpload>(`/v1/knowledge.upload?${q}`, file, signal);
}

export interface KnowledgeIngestJob {
  ingestion_id: string;
  state: string;
  source_uri: string;
}

/** What `knowledge.ingest` publishes an upload as. */
export interface KnowledgeIngestFields {
  source_uri: string;
  title?: string;
  description?: string;
  tags?: string[];
  allow_partial?: boolean;
  /** Publish only over this revision (0: only when none is live, else `revision_conflict`);
   *  unset publishes over any. */
  expected_revision?: number;
}

/** Have the server's extractor parse an upload and publish it as `source_uri`'s next revision. */
export async function ingestKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  opts: KnowledgeIngestFields & { upload_id: string },
): Promise<KnowledgeIngestJob> {
  return post<KnowledgeIngestJob>("/v1/knowledge.ingest", {
    org: id.org,
    namespace: id.namespace,
    upload_id: opts.upload_id,
    source_uri: opts.source_uri,
    title: opts.title || "",
    description: opts.description || "",
    tags: opts.tags || [],
    allow_partial: !!opts.allow_partial,
    ...(opts.expected_revision === undefined ? {} : { expected_revision: opts.expected_revision }),
  });
}

export interface KnowledgeIngestStatus {
  found: boolean;
  ingestion_id: string;
  source_uri: string;
  /** pending, processing, ready, partial, failed or cancelled. */
  state: string;
  /** parse, embed or publish while processing. */
  stage: string;
  error_code: string;
  error_message: string;
  format: string;
  /** How much of the file made it into the document (`lost`: capabilities it could not serve). */
  coverage: { unit?: string; total?: number; extracted?: number; failed?: unknown[]; lost?: string[] } | null;
  warnings: string[];
  chunks: number;
  revision: number;
}

export async function knowledgeIngestStatus(
  id: Pick<Identity, "org" | "namespace">,
  ingestionId: string,
  opts: RequestOpts = {},
): Promise<KnowledgeIngestStatus> {
  return post<KnowledgeIngestStatus>(
    "/v1/knowledge.ingest_status",
    { org: id.org, namespace: id.namespace, ingestion_id: ingestionId },
    opts.signal,
  );
}

export const DATASET_TYPES = ["string", "number", "bool", "date", "datetime", "time", "duration"] as const;
export type DatasetType = (typeof DATASET_TYPES)[number];

/** Mirrors `server/knowledge/api/models_knowledge.py::DatasetOut` (the fields shown). */
export interface Dataset {
  dataset_id: string;
  name: string;
  sheet: string;
  snapshot_id: string;
  /** Whether a person approved the schema, or it waits for one. */
  schema_state: string;
  columns: { name: string; type: DatasetType; inferred: string }[];
  /** What the typing could not settle and asks a person. */
  clarifications: { code: string; column: string; detail: string }[];
  row_count: number;
  rejected_rows: number;
  approved_by: string;
}

/** POST /v1/knowledge.dataset_get: the typed tables an ingested file became. */
export async function getDatasets(
  id: Pick<Identity, "org" | "namespace">,
  sourceUri: string,
  opts: RequestOpts = {},
): Promise<{ found: boolean; datasets: Dataset[] }> {
  return post(
    "/v1/knowledge.dataset_get",
    { org: id.org, namespace: id.namespace, source_uri: sourceUri },
    opts.signal,
  );
}

/** POST /v1/knowledge.dataset_approve: approve the schema, retyping the columns named. */
export async function approveDataset(
  id: Pick<Identity, "org" | "namespace">,
  sourceUri: string,
  dataset: Dataset,
  columns: Record<string, DatasetType>,
): Promise<{ found: boolean; retyped: boolean }> {
  return post("/v1/knowledge.dataset_approve", {
    org: id.org,
    namespace: id.namespace,
    source_uri: sourceUri,
    dataset_id: dataset.dataset_id,
    snapshot_id: dataset.snapshot_id,
    columns,
  });
}

/** POST /v1/knowledge.ingest_cancel: a pending job stops at once, a running one before
 *  it publishes; one that already ended answers `cancelled: false` with its state. */
export async function cancelIngestion(
  id: Pick<Identity, "org" | "namespace">,
  ingestionId: string,
): Promise<{ found: boolean; state: string; cancelled: boolean }> {
  return post("/v1/knowledge.ingest_cancel", { org: id.org, namespace: id.namespace, ingestion_id: ingestionId });
}

/** How the Library shows an original file. An uploaded file is
 *  attacker-chosen bytes under an attacker-chosen type, and a `blob:` URL
 *  ignores the server's Content-Disposition, so only passive types are
 *  previewed: text (read as UTF-8 and drawn as a text node, never parsed) and
 *  raster images (an `<img>`). Everything else downloads: HTML, SVG, XML, PDF,
 *  unknown. Mirrors the server's allowlist in `download_routes.preview_type`. */
export type PreviewKind = "text" | "image" | "download";

const PREVIEW_TEXT_TYPES = new Set(["text/plain", "text/markdown", "text/csv", "application/json"]);
const PREVIEW_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

export function previewKind(contentType: string): PreviewKind {
  const base = contentType.split(";")[0].trim().toLowerCase();
  if (PREVIEW_TEXT_TYPES.has(base)) return "text";
  if (PREVIEW_IMAGE_TYPES.has(base)) return "image";
  return "download";
}

export interface InlinePreview {
  kind: PreviewKind;
  /** A `blob:` URL of an image's bytes re-typed to its allowlisted type; null for
   *  `text` and `download`, which never get a URL. */
  url: string | null;
  /** A `text` file's first PREVIEW_TEXT_BYTES decoded as UTF-8; null for the other kinds. */
  text: string | null;
  contentType: string;
  size: number;
}

/** The most of a text file the preview decodes and shows: the rest is the Download's. */
export const PREVIEW_TEXT_BYTES = 1 << 20;

/** Fetch the original uploaded bytes for the preview pane. An img tag can't
 *  carry custom headers, so the auth'd fetch happens here; the caller revokes
 *  `url` when done. The kind comes from `previewKind`, and an image's blob
 *  type from its allowlist, never from the response. A text file's first
 *  PREVIEW_TEXT_BYTES are decoded. Returns null when the document has no
 *  preserved blob; throws on auth / 403 / unexpected errors. */
export async function fetchKnowledgePreview(
  id: Pick<Identity, "org" | "namespace">,
  sourceUri: string,
): Promise<InlinePreview | null> {
  const qs = new URLSearchParams({
    org: id.org,
    namespace: id.namespace,
    source_uri: sourceUri,
    inline: "true",
  });
  let r: Response;
  try {
    r = await getResponse(`/v1/knowledge.download?${qs}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
  const blob = await r.blob();
  const contentType = r.headers.get("Content-Type") || "application/octet-stream";
  const kind = previewKind(contentType);
  // `stream` holds back a character the cap cut in two instead of decoding it as U+FFFD.
  const text =
    kind === "text"
      ? new TextDecoder().decode(await blob.slice(0, PREVIEW_TEXT_BYTES).arrayBuffer(), { stream: true })
      : null;
  const url =
    kind === "image" ? URL.createObjectURL(new Blob([blob], { type: contentType.split(";")[0].trim() })) : null;
  return { kind, url, text, contentType, size: blob.size };
}

/** The file name a text-only document downloads under when the server
 * sends none: the last `source_uri` segment with `.md`. Mirrors the server's
 *  `text_download_filename`. */
export function textDownloadName(sourceUri: string): string {
  const segment = sourceUri.replace(/\/+$/, "").split("/").pop() || "";
  const slug = segment
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, 80);
  if (!slug) return "document.md";
  return slug.toLowerCase().endsWith(".md") ? slug : `${slug}.md`;
}

/** Trigger a download of a document: the original uploaded file when one
 *  was preserved, otherwise its stored text as Markdown (the server says
 * which in `X-Loom-Download-Source: blob|text`). GET-shaped so the browser's normal
 *  download flow handles the response; we kick it via a fetch + blob URL so the bearer
 *  token can be sent in a header. */
export async function downloadKnowledge(
  id: Pick<Identity, "org" | "namespace">,
  sourceUri: string,
  fallbackFilename: string,
): Promise<void> {
  const qs = new URLSearchParams({
    org: id.org,
    namespace: id.namespace,
    source_uri: sourceUri,
  });
  const r = await getResponse(`/v1/knowledge.download?${qs}`);
  // Prefer the server's filename, fall back to the caller's (the original
  // filename, or `<last URI segment>.md` for a text-only document).
  const disposition = r.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const filename = match ? match[1] : fallbackFilename;
  // Re-typed so the blob: URL can never render as a page, even opened on its own.
  const blob = new Blob([await r.blob()], { type: "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Defer revoke so the browser has time to dispatch the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ===== Curated-doc import =====

export interface ImportCuratedRow {
  memory_key: string;
  section_heading: string;
  bytes_written: number;
  verdict: "written" | "merged" | "duplicate" | "rejected_pinned" | "skipped";
  rejection_reason: string;
  pinned_blocker_id: string | null;
}

export interface ImportCuratedResult {
  rows: ImportCuratedRow[];
  written: number;
  merged: number;
  rejected: number;
  skipped: number;
}

/** Split a curated markdown doc into per-section memories. Each section
 *  becomes one memory_key=value pair, written via the same path as
 *  memory.set so pinned-conflict rejects fire per-row. */
export async function importCuratedDoc(
  id: Pick<Identity, "org" | "namespace">,
  opts: {
    content: string;
    source_uri?: string;
    heading_level?: number;
    extra_tags?: string[];
  },
): Promise<ImportCuratedResult> {
  return post("/v1/memory.import_curated", {
    org: id.org,
    namespace: id.namespace,
    content: opts.content,
    source_uri: opts.source_uri || "",
    heading_level: opts.heading_level ?? 2,
    extra_tags: opts.extra_tags || [],
  });
}
