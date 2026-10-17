import { startTransition, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
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
import { Chip, PageHeader, Tip, TipTarget } from "../components/ui";
import { Modal } from "../components/ui/Modal";
import { formatBytesShort, relativeTime } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { AddDocumentDialog } from "../flows/AddDocumentDialog";
import { EditDocumentMetadataDialog } from "../flows/EditDocumentMetadataDialog";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";
import { parseSearchQuery, removeQueryToken, toggleQueryToken } from "../lib/memoriesSearch";

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
  // controls nobody could use at once. Their slot keeps its width meanwhile, so the row
  // does not reflow on hover. A touch screen has no hover: there every row shows them.
  const [hotRow, setHotRow] = useState<string | null>(null);
  const [touch] = useState(() => typeof window.matchMedia === "function" && matchMedia("(pointer: coarse)").matches);
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

  // A tag filter is asked of the server (rows with any of the tags; the list then needs
  // every one), so it covers documents past the pages loaded so far. Debounced, so typing
  // `tag:…` sends one read.
  const queryTags = useMemo(() => parseSearchQuery(query).tags, [query]);
  const tagKey = JSON.stringify(queryTags);
  const [serverTags, setServerTags] = useState(tagKey);
  useEffect(() => {
    const t = setTimeout(() => setServerTags(tagKey), 250);
    return () => clearTimeout(t);
  }, [tagKey]);
  const shownScope = useRef("");

  const load = (signal?: AbortSignal, live: () => boolean = () => true) => {
    scopeRef.current += 1;
    // Blank only for a new namespace; a new filter keeps the rows until the next land.
    const scope = `${identity.org}/${identity.namespace}`;
    if (shownScope.current !== scope) {
      shownScope.current = scope;
      setRows(null);
    }
    setNextCursor(null);
    const tags = (JSON.parse(serverTags) as string[]).flatMap((t) => [t, t.toLowerCase()]);
    listKnowledge(
      { org: identity.org, namespace: identity.namespace },
      { signal, fields: LIBRARY_LIST_FIELDS, ...(tags.length ? { tags: [...new Set(tags)], limit: 500 } : {}) },
    )
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
  useAbortableEffect(load, [identity.org, identity.namespace, serverTags]);

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    const scope = scopeRef.current;
    setLoadingMore(true);
    try {
      const r = await listKnowledge(
        { org: identity.org, namespace: identity.namespace },
        {
          cursor: nextCursor,
          fields: LIBRARY_LIST_FIELDS,
          ...(queryTags.length
            ? { tags: [...new Set(queryTags.flatMap((t) => [t, t.toLowerCase()]))], limit: 500 }
            : {}),
        },
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
        return requiredTags.every((t) => lower.includes(t));
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
      <PageHeader
        title="Library"
        actions={
          <>
            <span role="status" className="text-sm text-text-muted tabular">
              {rows
                ? visibleRows && visibleRows.length !== rows.length
                  ? `${visibleRows.length} of ${rows.length} shown`
                  : `showing ${rows.length}`
                : ""}
            </span>
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
              Add document
            </Button>
          </>
        }
      >
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <p className="text-sm text-text-muted mb-6">
        Reference docs your agents can read. Memories are what the agent remembered; the library is what you gave it to
        read.
      </p>

      {rows !== null && rows.length > 0 && (
        <>
          {/* The search and the filters in force stay at the top as the list scrolls, so a
              tag picked from a row further down is seen to apply. */}
          <search aria-label="Search and filters" className="sticky top-0 z-[2] -mx-2 mb-3 bg-bg px-2 py-2">
            <SearchField
              value={query}
              onChange={(value) => setQuery(value)}
              placeholder="Search title, filename, description, or tag — try `tag:specs` or `tag:specs onboarding`"
              aria-label="Search library"
            />
            {queryTags.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
                <span className="text-text-muted">Filtering:</span>
                {queryTags.map((t) => (
                  <Chip
                    key={t}
                    label={`tag:${t}`}
                    mono
                    onRemove={() => setQuery((q) => removeQueryToken(q, `tag:${t}`))}
                  />
                ))}
                <Button type="empty" htmlType="button" onClick={() => setQuery(parseSearchQuery(query).text)}>
                  Clear filters
                </Button>
              </div>
            )}
            {selected.size > 0 && (
              <div className="mt-2 animate-fade-in flex items-center gap-2 p-2.5 rounded-xl border border-accent bg-accent-soft shadow-sm">
                <span className="text-sm font-medium text-accent tabular">{selected.size} selected</span>
                <div className="flex-1" />
                <Button type="secondary" htmlType="button" iconLeft="cross" onClick={clearSelection}>
                  Clear
                </Button>
                <Button type="danger" htmlType="button" iconLeft="trash" loading={bulkBusy} onClick={onBulkDelete}>
                  Delete all
                </Button>
              </div>
            )}
          </search>
          <div className="mb-5 flex justify-end">
            <Select
              value={sortBy}
              onSelect={(value) => {
                const v = oneOf(
                  SORTS.map((o) => o.value),
                  value,
                );
                if (v !== null) setSortBy(v);
              }}
              triggerProps={{ "aria-label": "Sort documents" }}
              className="w-44"
            >
              {SORTS.map((o) => (
                <Select.Item key={o.value} value={o.value}>
                  {o.label}
                </Select.Item>
              ))}
            </Select>
          </div>
        </>
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
          icon="document"
          title="Your library is empty"
          description="Upload PDFs, DOCX, Markdown or any text-shaped file. Your agents can search the extracted text via knowledge.search, and you can download the originals back any time."
          action={{ label: "Add document", onClick: () => setAddOpen(true) }}
        />
      )}

      {rows !== null && rows.length > 0 && visibleRows && visibleRows.length === 0 && (
        <div className="py-12 text-center">
          <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-surface-2">
            <Icon name="search" size="md" aria-hidden="true" />
          </div>
          <h2>No documents match</h2>
          <p className="mt-1 text-sm text-text-muted">
            {rows.length} loaded from this namespace, but none pass the active filters.
          </p>
          <Button type="secondary" htmlType="button" onClick={() => setQuery("")} className="mt-4">
            Clear search
          </Button>
        </div>
      )}

      {rows !== null && rows.length > 0 && visibleRows && visibleRows.length > 0 && (
        <div className="space-y-2">
          {(firstScreenOnly ? visibleRows.slice(0, FIRST_SCREEN_ROWS) : visibleRows).map((r) => (
            // biome-ignore lint/a11y/noStaticElementInteractions: tracks the row under the pointer or holding focus for the row actions; nothing happens on these events alone
            // biome-ignore lint/a11y/noNoninteractiveElementInteractions: as above
            <div
              key={r.source_uri}
              onMouseEnter={() => setHotRow(r.source_uri)}
              onMouseLeave={() => setHotRow((cur) => (cur === r.source_uri ? null : cur))}
              onFocus={() => setHotRow(r.source_uri)}
              data-doc-row
              className={`p-3.5 rounded-xl border bg-surface flex flex-wrap items-start gap-3 transition-colors [content-visibility:auto] [contain-intrinsic-size:auto_96px] ${
                selected.has(r.source_uri)
                  ? "border-accent ring-1 ring-accent/40"
                  : "border-border hover:border-border-strong"
              }`}
            >
              <Checkbox
                checked={selected.has(r.source_uri)}
                onCheckedChange={() => toggleSelected(r.source_uri)}
                aria-label={`Select ${r.title || r.original_filename || r.source_uri}`}
                className="mt-1 shrink-0"
                onClick={(e) => e.stopPropagation()}
              />
              <Icon name="document" size="sm" aria-hidden="true" className="shrink-0 mt-0.5" />
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => onOpen(r)}
                  className="w-full text-left bg-transparent border-0 p-0 cursor-pointer"
                  aria-label={`Preview ${r.title || r.original_filename || r.source_uri}`}
                >
                  <div className={`text-sm text-text font-medium truncate ${r.title ? "" : "font-mono"}`}>
                    {r.title || r.original_filename || r.source_uri}
                  </div>
                  {r.title && (r.original_filename || r.source_uri) && (
                    <div className="text-xs text-text-muted font-mono truncate">
                      {r.original_filename || r.source_uri}
                    </div>
                  )}
                  {r.description && (
                    <div className="text-xs text-text mt-1 line-clamp-2 leading-snug">{r.description}</div>
                  )}
                </button>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-text-muted tabular">
                  <span>{r.chunk_count} chunks</span>
                  <span>·</span>
                  <span>{relativeTime(r.ingested_at)}</span>
                  {r.superseded && (
                    <TipTarget tip="A newer version of this URI was uploaded; agents read the latest">
                      <Badge text="Superseded" state="warning" size="sm" />
                    </TipTarget>
                  )}
                  {r.tags && r.tags.length > 0 && (
                    <span className="flex flex-wrap items-center gap-1 sm:ml-2">
                      {r.tags.slice(0, 3).map((t) => (
                        <Chip
                          key={t}
                          label={`#${t}`}
                          ariaLabel={`Filter by tag ${t}`}
                          selected={queryTags.some((q) => q.toLowerCase() === t.toLowerCase())}
                          onClick={() => setQuery((cur) => toggleQueryToken(cur, `tag:${t}`))}
                        />
                      ))}
                    </span>
                  )}
                </div>
              </div>
              <div className="doc-acts">
                {(touch || hotRow === r.source_uri || selected.has(r.source_uri)) && (
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
                      label={hasOriginalFile(r) ? "Download original file" : "Download text (.md)"}
                      tooltip={
                        hasOriginalFile(r)
                          ? r.original_filename
                            ? `Download ${r.original_filename}${r.blob_size ? ` (${formatBytesShort(r.blob_size)})` : ""}`
                            : "Download original file"
                          : `Download text (.md) — ${textDownloadName(r.source_uri)}`
                      }
                      icon="download"
                      size="sm"
                    />
                    <IconButton
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditEntry(r);
                      }}
                      label="Edit details"
                      tooltip="Edit details (title, description, tags — no re-upload)"
                      icon="pencil"
                      size="sm"
                    />
                    <IconButton
                      onClick={(e) => {
                        e.stopPropagation();
                        setReplaceEntry(r);
                      }}
                      label="Edit content"
                      tooltip={
                        r.has_blob
                          ? `Edit content — upload a new file version of ${r.title || r.original_filename || r.source_uri}`
                          : "Edit content — upload the source file to enable download and preserve the original"
                      }
                      icon="refresh"
                      size="sm"
                    />
                    <IconButton
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(r.source_uri);
                      }}
                      label="Delete document"
                      icon="trash"
                      type="danger"
                      size="sm"
                    />
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {aggregateStats && rows && rows.length > 0 && (
        <div className="mt-8 pt-4 border-t border-border text-xs text-text-muted tabular flex items-center justify-between flex-wrap gap-2">
          <span>
            showing <span className="text-text font-medium">{rows.length}</span>
            {" · "}
            <span className="text-text font-medium">{aggregateStats.chunks.toLocaleString()}</span> chunk
            {aggregateStats.chunks === 1 ? "" : "s"} indexed
            {aggregateStats.withBlob > 0 && (
              <>
                {" · "}
                <span className="text-text font-medium">{formatBytesShort(aggregateStats.bytes)}</span> stored (
                {aggregateStats.withBlob} file{aggregateStats.withBlob === 1 ? "" : "s"})
              </>
            )}
          </span>
          {aggregateStats.withBlob < rows.length && (
            <span className="text-text-muted">
              {rows.length - aggregateStats.withBlob} text-only · no preserved original
            </span>
          )}
          {nextCursor && (
            <Button type="secondary" htmlType="button" loading={loadingMore} onClick={loadMore}>
              Load more
            </Button>
          )}
        </div>
      )}

      <Modal
        open={!!previewEntry}
        onOpenChange={(v) => {
          if (!v) closePreview();
        }}
        title={previewEntry?.title || previewEntry?.original_filename || previewEntry?.source_uri || "Document"}
        className="max-w-[900px] w-full"
        initialFocus="dialog"
      >
        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0 text-xs text-text-muted flex items-center gap-2 flex-wrap">
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
              <Badge key={t} text={t} state="neutral" size="sm" />
            ))}
          </div>
          {previewEntry && (
            <div className="flex items-center gap-2 shrink-0">
              <Tip content="Upload a new file version of this document">
                <Button
                  type="secondary"
                  htmlType="button"
                  iconLeft="refresh"
                  onClick={() => {
                    const r = previewEntry;
                    closePreview();
                    setReplaceEntry(r);
                  }}
                >
                  Edit content
                </Button>
              </Tip>
              <Tip
                content={
                  hasOriginalFile(previewEntry) ? "Download the original file" : "Save the stored text as Markdown"
                }
              >
                <Button type="primary" htmlType="button" iconLeft="download" onClick={() => onDownload(previewEntry)}>
                  {hasOriginalFile(previewEntry) ? "Download" : "Download text (.md)"}
                </Button>
              </Tip>
            </div>
          )}
        </div>
        {/* Sized to what the dialog leaves after its header and footer, so only the pane scrolls. */}
        <div className="h-[max(12rem,calc(75vh-14rem))] overflow-hidden rounded-lg bg-surface-2 relative">
          {previewLoading && (
            <div className="absolute inset-0 grid place-items-center text-sm text-text-muted">Loading…</div>
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
              className="w-full h-full border-0 bg-bg"
            />
          )}
          {!previewLoading && inlineBlob?.kind === "download" && previewEntry?.has_blob && (
            <div className="absolute inset-0 grid place-items-center px-8 text-center">
              <div className="max-w-md">
                <Icon name="document" size="xl" aria-hidden="true" className="mx-auto mb-3" />
                <div className="text-sm font-medium text-text">No preview for this file type</div>
                <div className="text-xs text-text-muted mt-2">
                  Only text and images are shown here. Click <strong>Download</strong> above to open the file.
                </div>
              </div>
            </div>
          )}
          {!previewLoading && !inlineBlob && previewEntry && !previewEntry.has_blob && (
            <div className="absolute inset-0 grid place-items-center px-8 text-center">
              <div className="max-w-md">
                <Icon name="document" size="xl" aria-hidden="true" className="mx-auto mb-3" />
                <div className="text-sm font-medium text-text">No original file preserved</div>
                <div className="text-xs text-text-muted mt-2">
                  This document was added as text (or uploaded before file preservation was enabled).
                  <strong>Download text (.md)</strong> above saves its text as Markdown; re-upload it to attach an
                  original file.
                </div>
                {previewEntry && (
                  <Button
                    type="secondary"
                    htmlType="button"
                    iconLeft="refresh"
                    className="mt-4"
                    onClick={() => {
                      const r = previewEntry;
                      closePreview();
                      setReplaceEntry(r);
                    }}
                  >
                    Re-upload
                  </Button>
                )}
              </div>
            </div>
          )}
          {!previewLoading && previewEntry?.has_blob && !inlineBlob && (
            <div className="absolute inset-0 grid place-items-center px-8 text-center">
              <div className="max-w-md">
                <Icon name="document" size="xl" aria-hidden="true" className="mx-auto mb-3" />
                <div className="text-sm font-medium text-text">Couldn't load preview</div>
                <div className="text-xs text-text-muted mt-2">
                  The original file is preserved but couldn't be rendered inline. Click <strong>Download</strong> above
                  to fetch it directly.
                </div>
              </div>
            </div>
          )}
        </div>
        <div className="mt-4 border-t border-border">
          <button
            type="button"
            aria-expanded={showText}
            onClick={() => setShowText((v) => !v)}
            className="w-full flex items-center justify-between px-1 py-2.5 text-xs text-text-muted hover:text-text hover:bg-surface-2 transition-colors"
          >
            <span>Extracted text (what your agents search)</span>
            <Icon name={showText ? "chevron-down" : "chevron-right"} size="xs" aria-hidden="true" />
          </button>
          {showText && previewDoc && previewDoc.found && (
            <pre className="whitespace-pre-wrap text-xs leading-normal text-text-muted font-sans px-1 pb-4">
              {previewDoc.content || "(no extracted text)"}
            </pre>
          )}
          {showText && previewDoc && !previewDoc.found && (
            <div className="text-xs text-text-muted px-1 pb-4">
              Document not found — it may have been deleted in another tab.
            </div>
          )}
        </div>
      </Modal>
    </PageContainer>
  );
}

/** Whether a document has an original file to download. False for a
 *  text-only document (no blob, size 0, no filename), whose Download
 * button saves the stored text as Markdown instead. */
function hasOriginalFile(r: KnowledgeEntry): boolean {
  return Boolean(r.has_blob || r.blob_size || r.original_filename);
}
