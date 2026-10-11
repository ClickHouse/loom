import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { FileText, Trash2, Plus, Download, X as XIcon, RefreshCw, Pencil, Search, ArrowUpDown } from "lucide-react";
import {
  listKnowledge,
  deleteKnowledge,
  bulkDeleteKnowledge,
  downloadKnowledge,
  getKnowledge,
  fetchKnowledgeInlineUrl,
  textDownloadName,
  type KnowledgeEntry,
  type KnowledgeDocument,
  type InlinePreview,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { SkeletonCard } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { toast, toastError } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { formatBytesShort, relativeTime } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { AddDocumentDialog } from "../flows/AddDocumentDialog";
import { EditDocumentMetadataDialog } from "../flows/EditDocumentMetadataDialog";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";

const FIRST_SCREEN_ROWS = 12;
type SortBy = "recency" | "title" | "chunks" | "size";
const SORTS: ReadonlyArray<{ value: SortBy; label: string }> = [
  { value: "recency", label: "Newest first" },
  { value: "title", label: "Title (A–Z)" },
  { value: "chunks", label: "Most chunks" },
  { value: "size", label: "Largest first" },
];
// The eleven fields a Library row and its dialogs read. Unprojected, a page of a hundred
// is 72 kB, a third of it the content_preview no row renders (the row shows the
// description).
const LIBRARY_LIST_FIELDS = [
  "source_uri",
  "title",
  "description",
  "tags",
  "ingested_at",
  "chunk_count",
  "superseded",
  "has_blob",
  "blob_size",
  "blob_content_type",
  "original_filename",
];

export function Documents() {
  const identity = useIdentity();
  const pendingDocSourceUri = useStore((s) => s.pendingDocSourceUri);
  const clearPendingDoc = useStore((s) => s.clearPendingDoc);
  const [rows, setRows] = useState<KnowledgeEntry[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState<SortBy>("recency");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // A row's four icon actions (each a tooltip'd button with an SVG) are rendered only for
  // the row under the pointer, focused, or selected — 50 rows carried ~30 nodes each for
  // controls nobody could use at once.
  const [hotRow, setHotRow] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [previewEntry, setPreviewEntry] = useState<KnowledgeEntry | null>(null);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [inlineBlob, setInlineBlob] = useState<InlinePreview | null>(null);
  const [showText, setShowText] = useState(false);
  const [replaceEntry, setReplaceEntry] = useState<KnowledgeEntry | null>(null);
  const [editEntry, setEditEntry] = useState<KnowledgeEntry | null>(null);

  // knowledge.list pages at 100: `nextCursor` is the server's continuation, null once the
  // namespace is fully loaded. `scopeRef` stamps each first-page load so a "Load more"
  // that resolves after the scope moved on is dropped instead of appended under the wrong
  // header.
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const scopeRef = useRef(0);

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    scopeRef.current += 1;
    setRows(null);
    setNextCursor(null);
    listKnowledge({ org: identity.org, namespace: identity.namespace }, { signal, fields: LIBRARY_LIST_FIELDS })
      .then((r) => {
        if (live()) {
          setRows(r.rows || []);
          setNextCursor(r.next_cursor ?? null);
        }
      })
      .catch(() => {
        if (live()) setRows([]);
      });
  };

  // A memory write cannot change the document list.
  useAbortableEffect(load, [identity.org, identity.namespace]);

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    const scope = scopeRef.current;
    setLoadingMore(true);
    try {
      const r = await listKnowledge(
        { org: identity.org, namespace: identity.namespace },
        { cursor: nextCursor, fields: LIBRARY_LIST_FIELDS },
      );
      if (scopeRef.current !== scope) return;
      setRows((prev) => [...(prev || []), ...(r.rows || [])]);
      setNextCursor(r.next_cursor ?? null);
    } catch (e) {
      if (scopeRef.current === scope) toastError("Load more failed", e);
    } finally {
      if (scopeRef.current === scope) setLoadingMore(false);
    }
  };

  useEffect(() => {
    if (!pendingDocSourceUri || !rows) return;
    const target = rows.find((r) => r.source_uri === pendingDocSourceUri);
    if (target) void onOpenRef.current(target);
    clearPendingDoc();
  }, [pendingDocSourceUri, rows, clearPendingDoc]);

  const onDelete = async (source_uri: string) => {
    if (
      !(await confirmAction({
        title: "Delete this document?",
        message:
          "All chunks for this URI will be tombstoned. The original file (if uploaded) gets removed too. This isn't reversible — you'd have to re-upload to bring it back.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    try {
      await deleteKnowledge({ org: identity.org, namespace: identity.namespace }, source_uri);
      toast("Deleted", "ok");
      load();
    } catch (e) {
      toastError("Delete failed", e);
    }
  };

  const onDownload = async (r: KnowledgeEntry) => {
    try {
      await downloadKnowledge(
        { org: identity.org, namespace: identity.namespace },
        r.source_uri,
        r.original_filename || textDownloadName(r.source_uri),
      );
    } catch (e) {
      toastError("Download failed", e);
    }
  };

  const onOpen = async (r: KnowledgeEntry) => {
    setPreviewEntry(r);
    setPreviewDoc(null);
    setShowText(false);
    if (inlineBlob?.url) {
      try {
        URL.revokeObjectURL(inlineBlob.url);
      } catch {
        /* */
      }
    }
    setInlineBlob(null);
    setPreviewLoading(true);
    try {
      const docPromise = getKnowledge({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch((e) => {
        toastError("Open failed", e);
        return null;
      });
      const blobPromise = r.has_blob
        ? fetchKnowledgeInlineUrl({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch(() => null)
        : Promise.resolve(null);
      const [d, blob] = await Promise.all([docPromise, blobPromise]);
      setPreviewDoc(d);
      setInlineBlob(blob);
    } finally {
      setPreviewLoading(false);
    }
  };
  // The deep-link effect opens with the latest render's `onOpen` without re-firing on it.
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;

  const closePreview = () => {
    if (inlineBlob?.url) {
      try {
        URL.revokeObjectURL(inlineBlob.url);
      } catch {
        /* */
      }
    }
    setInlineBlob(null);
    setPreviewEntry(null);
    setPreviewDoc(null);
    setShowText(false);
  };

  // The first screen mounts with the page, the rest of the rows in a transition after the
  // first frame — the Memories rule for the Library's fifty-row template (280 ms of long
  // tasks at 4x CPU).
  const [firstScreenOnly, setFirstScreenOnly] = useState(true);
  useEffect(() => {
    if (!firstScreenOnly || rows === null) return;
    const raf = requestAnimationFrame(() => {
      startTransition(() => setFirstScreenOnly(false));
    });
    return () => cancelAnimationFrame(raf);
  }, [firstScreenOnly, rows]);
  const visibleRows = useMemo(() => {
    if (!rows) return rows;
    let r = rows;
    const tokens = query.trim().split(/\s+/).filter(Boolean);
    const requiredTags: string[] = [];
    const freeText: string[] = [];
    for (const tok of tokens) {
      const m = /^tag:(.+)$/i.exec(tok);
      if (m) requiredTags.push(m[1].toLowerCase());
      else freeText.push(tok.toLowerCase());
    }
    if (requiredTags.length > 0) {
      r = r.filter((row) => {
        const lower = (row.tags || []).map((t) => t.toLowerCase());
        return requiredTags.every((t) => lower.some((x) => x.includes(t)));
      });
    }
    if (freeText.length > 0) {
      r = r.filter((row) => {
        const haystack = [
          row.title || "",
          row.original_filename || "",
          row.source_uri || "",
          row.description || "",
          ...(row.tags || []),
        ]
          .join(" ")
          .toLowerCase();
        return freeText.every((t) => haystack.includes(t));
      });
    }
    const sorted = [...r];
    if (sortBy === "recency") {
      sorted.sort((a, b) => (b.ingested_at || "").localeCompare(a.ingested_at || ""));
    } else if (sortBy === "title") {
      const key = (row: KnowledgeEntry) => (row.title || row.original_filename || row.source_uri || "").toLowerCase();
      sorted.sort((a, b) => key(a).localeCompare(key(b)));
    } else if (sortBy === "chunks") {
      sorted.sort((a, b) => (b.chunk_count || 0) - (a.chunk_count || 0));
    } else if (sortBy === "size") {
      sorted.sort((a, b) => (b.blob_size || 0) - (a.blob_size || 0));
    }
    return sorted;
  }, [rows, query, sortBy]);

  const toggleSelected = (uri: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const onBulkDelete = async () => {
    if (selected.size === 0 || bulkBusy) return;
    if (
      !(await confirmAction({
        title: `Delete ${selected.size} document${selected.size === 1 ? "" : "s"}?`,
        message:
          "All chunks for the selected URIs will be tombstoned and their original files removed. Not reversible — you'd have to re-upload to bring them back.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    setBulkBusy(true);
    try {
      const r = await bulkDeleteKnowledge({ org: identity.org, namespace: identity.namespace }, Array.from(selected));
      toast(`Deleted ${r.total_tombstoned} document${r.total_tombstoned === 1 ? "" : "s"}`, "ok");
      clearSelection();
      load();
    } catch (e) {
      toastError("Bulk delete failed", e);
    } finally {
      setBulkBusy(false);
    }
  };

  const aggregateStats = useMemo(() => {
    if (!rows) return null;
    let chunks = 0;
    let bytes = 0;
    let withBlob = 0;
    for (const r of rows) {
      chunks += r.chunk_count || 0;
      if (r.has_blob) {
        withBlob += 1;
        bytes += r.blob_size || 0;
      }
    }
    return { chunks, bytes, withBlob };
  }, [rows]);

  return (
    <PageContainer>
      <div className="flex items-baseline justify-between mb-1">
        <h1 className="text-3xl font-semibold tracking-tight">Library</h1>
        <div className="flex items-center gap-3">
          <span className="text-sm text-[var(--color-text-muted)] tabular">
            {rows
              ? visibleRows && visibleRows.length !== rows.length
                ? `${visibleRows.length} of ${rows.length} shown`
                : `showing ${rows.length}`
              : ""}
          </span>
          <button
            onClick={() => setAddOpen(true)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-[var(--color-accent)] text-[var(--color-on-accent)] rounded-md hover:bg-[var(--color-accent-strong)] transition-colors"
          >
            <Plus size={13} /> Add document
          </button>
        </div>
      </div>
      <div className="mb-3">
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </div>
      <p className="text-sm text-[var(--color-text-muted)] mb-6">
        Reference docs your agents can read. Memories are what the agent remembered; the library is what you gave it to
        read.
      </p>

      {rows !== null && rows.length > 0 && (
        <>
          <div className="relative mb-3">
            <Search
              size={15}
              className="absolute left-3.5 top-1/2 -translate-y-1/2 text-[var(--color-text-muted)] pointer-events-none"
              aria-hidden="true"
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search title, filename, description, or tag — try `tag:specs` or `tag:specs onboarding`"
              aria-label="Search library"
              className="w-full pl-10 pr-9 py-2.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] text-[var(--color-text)] placeholder:text-[var(--color-text-subtle)] text-base focus:border-[var(--color-accent)] outline-none transition-colors"
            />
            {query && (
              <button
                onClick={() => setQuery("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] rounded"
                aria-label="Clear search"
              >
                <XIcon size={14} />
              </button>
            )}
          </div>
          <div className="mb-5 flex justify-end">
            <div className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-[var(--color-surface-2)]">
              <ArrowUpDown size={11} className="text-[var(--color-text-muted)]" />
              <select
                value={sortBy}
                onChange={(e) => {
                  const v = oneOf(
                    SORTS.map((o) => o.value),
                    e.target.value,
                  );
                  if (v !== null) setSortBy(v);
                }}
                aria-label="Sort documents"
                className="text-xs bg-transparent outline-none cursor-pointer text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              >
                {SORTS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </>
      )}

      {selected.size > 0 && (
        <div className="sticky top-0 z-[2] mb-3 animate-fade-in motion-reduce:animate-none! flex items-center gap-2 p-2.5 rounded-xl border border-[var(--color-accent)] bg-[var(--color-accent-soft)] shadow-sm">
          <span className="text-sm font-medium text-[var(--color-accent)] tabular">{selected.size} selected</span>
          <div className="flex-1" />
          <button
            onClick={onBulkDelete}
            disabled={bulkBusy}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md text-danger bg-danger/15 hover:bg-danger/25 disabled:opacity-50"
          >
            <Trash2 size={11} /> Delete all
          </button>
          <button
            onClick={clearSelection}
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)]"
          >
            <XIcon size={11} /> Clear
          </button>
        </div>
      )}

      {/* Mounted while open, not on every render. */}
      {addOpen && (
        <AddDocumentDialog
          open={addOpen}
          onOpenChange={setAddOpen}
          identity={{ org: identity.org, namespace: identity.namespace }}
          onAdded={load}
        />
      )}
      {!!replaceEntry && (
        <AddDocumentDialog
          open={!!replaceEntry}
          onOpenChange={(v) => {
            if (!v) setReplaceEntry(null);
          }}
          identity={{ org: identity.org, namespace: identity.namespace }}
          onAdded={load}
          replaceMode={
            replaceEntry
              ? {
                  sourceUri: replaceEntry.source_uri,
                  title: replaceEntry.title,
                  description: replaceEntry.description,
                  tags: replaceEntry.tags,
                }
              : undefined
          }
        />
      )}
      <EditDocumentMetadataDialog
        open={!!editEntry}
        onOpenChange={(v) => {
          if (!v) setEditEntry(null);
        }}
        identity={{ org: identity.org, namespace: identity.namespace }}
        entry={editEntry}
        onSaved={load}
      />

      {rows === null && (
        <div className="space-y-2">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}

      {rows !== null && rows.length === 0 && (
        <EmptyState
          icon={FileText}
          title="Your library is empty"
          description="Upload PDFs, DOCX, Markdown or any text-shaped file. Your agents can search the extracted text via knowledge.search, and you can download the originals back any time."
          action={{ label: "Add document", onClick: () => setAddOpen(true) }}
        />
      )}

      {rows !== null && rows.length > 0 && visibleRows && visibleRows.length === 0 && (
        <div className="py-12 text-center">
          <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
            <Search size={20} strokeWidth={1.5} />
          </div>
          <h2 className="text-lg font-semibold tracking-tight">No documents match</h2>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            {rows.length} loaded from this namespace, but none pass the active filters.
          </p>
          <button
            onClick={() => setQuery("")}
            className="mt-4 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] transition-colors"
          >
            Clear search
          </button>
        </div>
      )}

      {rows !== null && rows.length > 0 && visibleRows && visibleRows.length > 0 && (
        <div className="space-y-2">
          {(firstScreenOnly ? visibleRows.slice(0, FIRST_SCREEN_ROWS) : visibleRows).map((r) => (
            <div
              key={r.source_uri}
              onMouseEnter={() => setHotRow(r.source_uri)}
              onMouseLeave={() => setHotRow((cur) => (cur === r.source_uri ? null : cur))}
              onFocus={() => setHotRow(r.source_uri)}
              className={`p-3.5 rounded-xl border bg-[var(--color-surface)] flex items-start gap-3 transition-colors [content-visibility:auto] [contain-intrinsic-size:auto_96px] ${
                selected.has(r.source_uri)
                  ? "border-[var(--color-accent)] ring-1 ring-[var(--color-accent)]/40"
                  : "border-[var(--color-border)] hover:border-[var(--color-border-strong)]"
              }`}
            >
              <input
                type="checkbox"
                checked={selected.has(r.source_uri)}
                onChange={() => toggleSelected(r.source_uri)}
                aria-label={`Select ${r.title || r.original_filename || r.source_uri}`}
                className="mt-1 shrink-0 cursor-pointer"
                onClick={(e) => e.stopPropagation()}
              />
              <FileText size={16} className="text-[var(--color-text-muted)] shrink-0 mt-0.5" aria-hidden="true" />
              <button
                onClick={() => onOpen(r)}
                className="min-w-0 flex-1 text-left bg-transparent border-0 p-0 cursor-pointer"
                aria-label={`Preview ${r.title || r.original_filename || r.source_uri}`}
              >
                <div
                  className={`text-base text-[var(--color-text)] font-medium truncate ${r.title ? "" : "font-mono"}`}
                >
                  {r.title || r.original_filename || r.source_uri}
                </div>
                {r.title && (r.original_filename || r.source_uri) && (
                  <div className="text-xs text-[var(--color-text-muted)] font-mono truncate">
                    {r.original_filename || r.source_uri}
                  </div>
                )}
                {r.description && (
                  <div className="text-xs text-[var(--color-text)] mt-1 line-clamp-2 leading-snug">{r.description}</div>
                )}
                <div className="mt-1.5 flex items-center gap-2 text-2xs text-[var(--color-text-muted)] tabular">
                  <span>{r.chunk_count} chunks</span>
                  <span>·</span>
                  <span>{relativeTime(r.ingested_at)}</span>
                  {r.superseded && (
                    <span
                      className="text-2xs px-1.5 py-0.5 rounded font-medium bg-warn/15 text-warn"
                      title="A newer version of this URI was uploaded; agents read the latest"
                    >
                      superseded
                    </span>
                  )}
                  {r.tags && r.tags.length > 0 && (
                    <span className="flex items-center gap-1 ml-2">
                      {r.tags.slice(0, 3).map((t) => (
                        <button
                          key={t}
                          onClick={(e) => {
                            e.stopPropagation();
                            const tagToken = `tag:${t}`;
                            setQuery((cur) => (cur.includes(tagToken) ? cur : cur ? `${cur} ${tagToken}` : tagToken));
                          }}
                          className="inline-flex items-center text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] hover:bg-[var(--color-accent-soft)] hover:text-[var(--color-accent)] transition-colors cursor-pointer"
                          title={`Filter by tag: ${t}`}
                        >
                          <span className="opacity-50 mr-0.5">#</span>
                          {t}
                        </button>
                      ))}
                    </span>
                  )}
                </div>
              </button>
              {(hotRow === r.source_uri || selected.has(r.source_uri)) && (
                <>
                  {/* Icon actions: ≥36 px targets, named by aria-label + tooltip.
                    "Edit content" also covers the no-blob case — uploading a file
                    into a text-only document is the same replace flow. Download is
                    offered for every document: the original file
                    when one was preserved, else the stored text as Markdown. */}
                  <IconButton
                    onClick={(e) => {
                      e.stopPropagation();
                      onDownload(r);
                    }}
                    className="text-[var(--color-text-muted)] hover:text-[var(--color-accent)] hover:bg-[var(--color-surface-2)]"
                    label={hasOriginalFile(r) ? "Download original file" : "Download text (.md)"}
                    tooltip={
                      hasOriginalFile(r)
                        ? r.original_filename
                          ? `Download ${r.original_filename}${r.blob_size ? ` (${formatBytesShort(r.blob_size)})` : ""}`
                          : "Download original file"
                        : `Download text (.md) — ${textDownloadName(r.source_uri)}`
                    }
                  >
                    <Download size={14} />
                  </IconButton>
                  <IconButton
                    onClick={(e) => {
                      e.stopPropagation();
                      setEditEntry(r);
                    }}
                    className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)]"
                    label="Edit details"
                    tooltip="Edit details (title, description, tags — no re-upload)"
                  >
                    <Pencil size={14} />
                  </IconButton>
                  <IconButton
                    onClick={(e) => {
                      e.stopPropagation();
                      setReplaceEntry(r);
                    }}
                    className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)]"
                    label="Edit content"
                    tooltip={
                      r.has_blob
                        ? `Edit content — upload a new file version of ${r.title || r.original_filename || r.source_uri}`
                        : "Edit content — upload the source file to enable download and preserve the original"
                    }
                  >
                    <RefreshCw size={14} />
                  </IconButton>
                  <IconButton
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(r.source_uri);
                    }}
                    className="text-[var(--color-danger)] hover:bg-[var(--color-surface-2)]"
                    label="Delete document"
                  >
                    <Trash2 size={14} />
                  </IconButton>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {aggregateStats && rows && rows.length > 0 && (
        <div className="mt-8 pt-4 border-t border-[var(--color-border)] text-xs text-[var(--color-text-muted)] tabular flex items-center justify-between flex-wrap gap-2">
          <span>
            showing <span className="text-[var(--color-text)] font-medium">{rows.length}</span>
            {" · "}
            <span className="text-[var(--color-text)] font-medium">{aggregateStats.chunks.toLocaleString()}</span> chunk
            {aggregateStats.chunks === 1 ? "" : "s"} indexed
            {aggregateStats.withBlob > 0 && (
              <>
                {" · "}
                <span className="text-[var(--color-text)] font-medium">{formatBytesShort(aggregateStats.bytes)}</span>{" "}
                stored ({aggregateStats.withBlob} file{aggregateStats.withBlob === 1 ? "" : "s"})
              </>
            )}
          </span>
          {aggregateStats.withBlob < rows.length && (
            <span className="text-[var(--color-text-subtle)]">
              {rows.length - aggregateStats.withBlob} text-only · no preserved original
            </span>
          )}
          {nextCursor && (
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium border border-[var(--color-border)] rounded-md hover:border-[var(--color-border-strong)] hover:bg-[var(--color-surface-2)] transition-colors disabled:opacity-50"
            >
              {loadingMore ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      )}

      <Dialog.Root
        open={!!previewEntry}
        onOpenChange={(v) => {
          if (!v) closePreview();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(900px,94vw)] h-[min(85vh,800px)] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
            <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
              <div className="min-w-0 flex-1">
                <Dialog.Title
                  className={`text-md font-semibold tracking-tight truncate ${previewEntry?.title ? "" : "font-mono"}`}
                >
                  {previewEntry?.title || previewEntry?.original_filename || previewEntry?.source_uri || "Document"}
                </Dialog.Title>
                <div className="text-2xs text-[var(--color-text-muted)] mt-1 flex items-center gap-2 flex-wrap">
                  {previewEntry?.ingested_at && <span>uploaded {relativeTime(previewEntry.ingested_at)}</span>}
                  {previewEntry?.has_blob && previewEntry.blob_size != null && (
                    <>
                      <span>·</span>
                      <span>{formatBytesShort(previewEntry.blob_size)}</span>
                    </>
                  )}
                  {previewEntry?.blob_content_type && (
                    <>
                      <span>·</span>
                      <span className="font-mono">{previewEntry.blob_content_type}</span>
                    </>
                  )}
                  {(previewEntry?.tags || []).slice(0, 4).map((t) => (
                    <span key={t} className="text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface-2)]">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {previewEntry && (
                  <button
                    onClick={() => {
                      const r = previewEntry;
                      closePreview();
                      setReplaceEntry(r);
                    }}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
                    title="Upload a new file version of this document"
                  >
                    <RefreshCw size={13} /> Edit content
                  </button>
                )}
                {previewEntry && (
                  <button
                    onClick={() => onDownload(previewEntry)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]"
                    title={hasOriginalFile(previewEntry) ? "Download original file" : "Download text (.md)"}
                  >
                    <Download size={13} /> {hasOriginalFile(previewEntry) ? "Download" : "Download text (.md)"}
                  </button>
                )}
                <Dialog.Close
                  className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                  aria-label="Close"
                >
                  <XIcon size={16} />
                </Dialog.Close>
              </div>
            </div>
            <div className="flex-1 overflow-hidden bg-[var(--color-surface-2)] relative">
              {previewLoading && (
                <div className="absolute inset-0 grid place-items-center text-sm text-[var(--color-text-muted)]">
                  Loading…
                </div>
              )}
              {/* Only passive types reach here (previewKind): text in an iframe whose empty
                  sandbox grants nothing, images as <img>. Nothing else is ever framed. */}
              {!previewLoading && inlineBlob?.url && previewEntry?.has_blob && inlineBlob.kind === "image" && (
                <div className="absolute inset-0 grid place-items-center p-6 overflow-auto">
                  <img
                    src={inlineBlob.url}
                    alt={previewEntry.original_filename || "document"}
                    className="max-w-full max-h-full object-contain shadow"
                  />
                </div>
              )}
              {!previewLoading && inlineBlob?.url && previewEntry?.has_blob && inlineBlob.kind === "text" && (
                <iframe
                  sandbox=""
                  src={inlineBlob.url}
                  title={previewEntry.original_filename || "document preview"}
                  className="w-full h-full border-0 bg-text"
                />
              )}
              {!previewLoading && inlineBlob?.kind === "download" && previewEntry?.has_blob && (
                <div className="absolute inset-0 grid place-items-center px-8 text-center">
                  <div className="max-w-md">
                    <FileText size={32} className="mx-auto text-[var(--color-text-subtle)] mb-3" />
                    <div className="text-base font-medium text-[var(--color-text)]">No preview for this file type</div>
                    <div className="text-xs text-[var(--color-text-muted)] mt-2">
                      Only text and images are shown here. Click <strong>Download</strong> above to open the file.
                    </div>
                  </div>
                </div>
              )}
              {!previewLoading && !inlineBlob && previewEntry && !previewEntry.has_blob && (
                <div className="absolute inset-0 grid place-items-center px-8 text-center">
                  <div className="max-w-md">
                    <FileText size={32} className="mx-auto text-[var(--color-text-subtle)] mb-3" />
                    <div className="text-base font-medium text-[var(--color-text)]">No original file preserved</div>
                    <div className="text-xs text-[var(--color-text-muted)] mt-2">
                      This document was added as text (or uploaded before file preservation was enabled).
                      <strong>Download text (.md)</strong> above saves its text as Markdown; re-upload it to attach an
                      original file.
                    </div>
                    {previewEntry && (
                      <button
                        onClick={() => {
                          const r = previewEntry;
                          closePreview();
                          setReplaceEntry(r);
                        }}
                        className="mt-4 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md bg-[var(--color-accent)] text-[var(--color-on-accent)] hover:bg-[var(--color-accent-strong)]"
                      >
                        <RefreshCw size={13} /> Re-upload
                      </button>
                    )}
                  </div>
                </div>
              )}
              {!previewLoading && previewEntry?.has_blob && !inlineBlob && (
                <div className="absolute inset-0 grid place-items-center px-8 text-center">
                  <div className="max-w-md">
                    <FileText size={32} className="mx-auto text-[var(--color-text-subtle)] mb-3" />
                    <div className="text-base font-medium text-[var(--color-text)]">Couldn't load preview</div>
                    <div className="text-xs text-[var(--color-text-muted)] mt-2">
                      The original file is preserved but couldn't be rendered inline. Click <strong>Download</strong>{" "}
                      above to fetch it directly.
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="border-t border-[var(--color-border)] shrink-0 max-h-[40%] overflow-y-auto">
              <button
                onClick={() => setShowText((v) => !v)}
                className="w-full flex items-center justify-between px-6 py-2.5 text-2xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] transition-colors"
              >
                <span>{showText ? "Hide" : "Show"} extracted text (what your agents search)</span>
                <span className="font-mono">{showText ? "▴" : "▾"}</span>
              </button>
              {showText && previewDoc && previewDoc.found && (
                <pre className="whitespace-pre-wrap text-xs leading-[1.5] text-[var(--color-text-muted)] font-sans px-6 pb-4">
                  {previewDoc.content || "(no extracted text)"}
                </pre>
              )}
              {showText && previewDoc && !previewDoc.found && (
                <div className="text-xs text-[var(--color-text-muted)] px-6 pb-4">
                  Document not found — it may have been deleted in another tab.
                </div>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </PageContainer>
  );
}

/** Whether a document has an original file to download. False for a
 *  text-only document (no blob, size 0, no filename), whose Download
 * button saves the stored text as Markdown instead. */
function hasOriginalFile(r: KnowledgeEntry): boolean {
  return Boolean(r.has_blob || r.blob_size || r.original_filename);
}
