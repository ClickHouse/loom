import { memo, startTransition, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
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
  fetchKnowledgePreview,
  PREVIEW_TEXT_BYTES,
  textDownloadName,
  type KnowledgeEntry,
  type KnowledgeDocument,
  type InlinePreview,
} from "../api";
import { setViewParam, useStore, useIdentity } from "../lib/state";
import { RowSkeleton } from "../components/Skeleton";
import { toast, toastError } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { ListRow, Loading, LoadingSkeleton, PageHeader, TagList, Tip } from "../components/ui";
import { BusyButton } from "../components/ui/BusyButton";
import { ListState } from "../components/ui/ListState";
import { ListTime } from "../components/ui/ListTime";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { Modal } from "../components/ui/Modal";
import { formatBytesShort } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { AddDocumentDialog } from "../flows/AddDocumentDialog";
import { EditDocumentMetadataDialog } from "../flows/EditDocumentMetadataDialog";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";
import { parseSearchQuery, toggleQueryToken } from "../lib/memoriesSearch";
import { useMedia, PHONE_QUERY } from "../lib/useMedia";
import { useRowNav } from "../lib/useRowNav";
import { useLastPageFocus } from "../lib/useLastPageFocus";
import { useRowSelection } from "../lib/useRowSelection";

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

// Documents per read; "Load more" asks for the next 50 (knowledge.list has a cursor, no total).
const LIBRARY_PAGE = 50;

/** The document at exactly `uri`, read through its prefix page by page (newer documents
 *  under the same prefix come first), or null when the namespace does not hold it. */
async function findDocument(
  id: { org: string; namespace: string },
  uri: string,
  signal: AbortSignal,
): Promise<KnowledgeEntry | null> {
  let cursor: string | undefined;
  do {
    const r = await listKnowledge(id, { uriPrefix: [uri], fields: LIBRARY_LIST_FIELDS, limit: 500, cursor, signal });
    const found = (r.rows || []).find((d) => d.source_uri === uri);
    if (found) return found;
    cursor = r.next_cursor ?? undefined;
  } while (cursor && !signal.aborted);
  return null;
}

export function Documents() {
  const identity = useIdentity();
  const phone = useMedia(PHONE_QUERY);
  const pendingDocSourceUri = useStore((s) => s.pendingDocSourceUri);
  const clearPendingDoc = useStore((s) => s.clearPendingDoc);
  const [rows, setRows] = useState<KnowledgeEntry[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const focusSearchOnRead = useRef(false);
  const [sortBy, setSortBy] = useState<SortBy>("recency");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [previewEntry, setPreviewEntry] = useState<KnowledgeEntry | null>(null);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewFile, setPreviewFile] = useState<InlinePreview | null>(null);
  const [showText, setShowText] = useState(false);
  const [replaceEntry, setReplaceEntry] = useState<KnowledgeEntry | null>(null);
  const [editEntry, setEditEntry] = useState<KnowledgeEntry | null>(null);

  // `nextCursor` is the server's continuation, null once the namespace is fully loaded.
  // `scopeRef` stamps each first-page load so a "Load more" that resolves after the scope
  // moved on is dropped instead of appended under the wrong header.
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const scopeRef = useRef(0);

  // A tag filter is asked of the server, which returns the documents carrying every tag
  // (a server a release behind matches any one, and the list then requires every tag
  // itself), so it covers documents past the pages loaded so far. Debounced, so typing
  // `tag:…` sends one read.
  // The list filters by the text a render behind the box, so a keystroke paints the box
  // first; the memoised rows render again only when what they show changes.
  const listQuery = useDeferredValue(query);
  const parsedQuery = useMemo(() => parseSearchQuery(listQuery), [listQuery]);
  const queryTags = parsedQuery.tags;
  // Built from the tags' text (a tag holds no space), so a keystroke that leaves the tags as
  // they were hands every row the same set.
  const activeTagsText = queryTags.map((t) => t.toLowerCase()).join(" ");
  const activeTags = useMemo(() => new Set(activeTagsText.split(" ").filter(Boolean)), [activeTagsText]);
  // Each tag once: the first page and Load more send the same list.
  const tagKey = JSON.stringify([...new Set(queryTags)]);
  const [serverTags, setServerTags] = useState(tagKey);
  useEffect(() => {
    const t = setTimeout(() => setServerTags(tagKey), 250);
    return () => clearTimeout(t);
  }, [tagKey]);
  const shownScope = useRef("");
  // The tags the rows on screen were read for, and why the last read failed.
  const [rowsFor, setRowsFor] = useState("");
  const [loadError, setLoadError] = useState<unknown>(null);
  // Every reload (Retry, a delete, an add, an edit) goes through the effect below, so an
  // answer for a namespace the page has left is aborted and dropped.
  const [reloads, setReloads] = useState(0);
  const reload = () => setReloads((n) => n + 1);

  const load = (signal: AbortSignal, live: () => boolean) => {
    scopeRef.current += 1;
    // Blank only for a new namespace; a new filter keeps the rows until the next land.
    const scope = `${identity.org}/${identity.namespace}`;
    if (shownScope.current !== scope) {
      shownScope.current = scope;
      setRows(null);
    }
    setNextCursor(null);
    const tags = JSON.parse(serverTags) as string[];
    listKnowledge(
      { org: identity.org, namespace: identity.namespace },
      {
        signal,
        fields: LIBRARY_LIST_FIELDS,
        limit: LIBRARY_PAGE,
        ...(tags.length ? { tags } : {}),
      },
    )
      .then((r) => {
        if (live()) {
          setLoadError(null);
          setRows(r.rows || []);
          setRowsFor(serverTags);
          setNextCursor(r.next_cursor ?? null);
        }
      })
      .catch((e) => {
        if (!live()) return;
        // A failed read is said as a failure, never as an empty library.
        setLoadError(e);
        setRows([]);
        setRowsFor(serverTags);
      });
  };

  // A memory write cannot change the document list.
  useAbortableEffect(load, [identity.org, identity.namespace, serverTags, reloads]);

  // The library is empty only when nothing narrowed the read and the read worked.
  const emptyLibrary = rows !== null && rows.length === 0 && !loadError && serverTags === "[]" && tagKey === serverTags;
  const readTags = JSON.parse(serverTags) as string[];
  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    const scope = scopeRef.current;
    markShown(shownRows?.map((r) => r.source_uri) ?? []);
    setLoadingMore(true);
    try {
      const r = await listKnowledge(
        { org: identity.org, namespace: identity.namespace },
        {
          cursor: nextCursor,
          fields: LIBRARY_LIST_FIELDS,
          limit: LIBRARY_PAGE,
          // The tags the list was read with, so the cursor and the filter agree.
          ...(readTags.length ? { tags: readTags } : {}),
        },
      );
      if (scopeRef.current !== scope) return;
      setRows((prev) => [...(prev || []), ...(r.rows || [])]);
      setNextCursor(r.next_cursor ?? null);
    } catch (e) {
      if (scopeRef.current === scope) toastError("Load more failed", e);
    } finally {
      // Always: a load for a scope the list has left must not leave the button spinning.
      setLoadingMore(false);
    }
  };

  // The last page removes Load more, which held the focus: it goes to the first document
  // in the list's order that the page added, else the last one shown, else the search.
  const listBox = useRef<HTMLDivElement | null>(null);
  const markShown = useLastPageFocus({
    loading: loadingMore,
    more: !!nextCursor,
    rows: () =>
      [...(listBox.current?.querySelectorAll<HTMLElement>("[data-doc-row]") ?? [])].flatMap((el) => {
        const open = el.querySelector<HTMLElement>("[data-row-open]");
        return open ? [[el.dataset.docRow ?? "", open] as [string, HTMLElement]] : [];
      }),
    fallback: () => searchRef.current,
  });

  // The read behind a `?doc=` link past the loaded rows. It ends with the page, with its
  // namespace, and when the reader opens or closes a document themselves: its answer then
  // opens nothing.
  const linkRead = useRef<AbortController | null>(null);
  useEffect(() => () => linkRead.current?.abort(), [identity.org, identity.namespace]);

  useEffect(() => {
    if (!pendingDocSourceUri || !rows) return;
    const uri = pendingDocSourceUri;
    clearPendingDoc();
    const target = rows.find((r) => r.source_uri === uri);
    if (target) {
      void handlers.current.onOpen(target);
      return;
    }
    // Not among the rows loaded (a link past the first page): read that one document. A
    // link to one the namespace does not hold opens nothing, and the URL stops naming it;
    // a failed read says so and keeps the link.
    linkRead.current?.abort();
    const ac = new AbortController();
    linkRead.current = ac;
    void findDocument({ org: identity.org, namespace: identity.namespace }, uri, ac.signal)
      .then((found) => {
        if (ac.signal.aborted) return;
        if (found) void handlers.current.onOpen(found);
        else setViewParam("doc", null);
      })
      .catch((e) => {
        if (!ac.signal.aborted) toastError("Open failed", e);
      });
  }, [pendingDocSourceUri, rows, clearPendingDoc, identity.org, identity.namespace]);
  // After a row's delete, the read that drops the row hands focus to the search field.
  useEffect(() => {
    if (!focusSearchOnRead.current) return;
    focusSearchOnRead.current = false;
    searchRef.current?.focus();
  }, [rows]);
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
      // The row and its menu leave with the next read: focus then goes to the search.
      focusSearchOnRead.current = true;
      reload();
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
    linkRead.current?.abort();
    // The open document is the page's URL (`?doc=`): a reload or Back reopens it.
    setViewParam("doc", r.source_uri);
    setPreviewEntry(r);
    setPreviewDoc(null);
    setShowText(false);
    if (previewFile?.url) {
      try {
        URL.revokeObjectURL(previewFile.url);
      } catch {
        /* */
      }
    }
    setPreviewFile(null);
    setPreviewLoading(true);
    try {
      const docPromise = getKnowledge({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch((e) => {
        toastError("Open failed", e);
        return null;
      });
      const blobPromise = r.has_blob
        ? fetchKnowledgePreview({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch(() => null)
        : Promise.resolve(null);
      const [d, blob] = await Promise.all([docPromise, blobPromise]);
      setPreviewDoc(d);
      setPreviewFile(blob);
    } finally {
      setPreviewLoading(false);
    }
  };
  // The deep-link effect and the rows call the latest render's handlers without re-firing
  // on them or rendering every row again.
  const handlers = useRef({ onOpen, onDownload, onDelete });
  handlers.current = { onOpen, onDownload, onDelete };

  const closePreview = () => {
    linkRead.current?.abort();
    if (previewFile?.url) {
      try {
        URL.revokeObjectURL(previewFile.url);
      } catch {
        /* */
      }
    }
    setPreviewFile(null);
    setPreviewEntry(null);
    setPreviewDoc(null);
    setShowText(false);
    setViewParam("doc", null);
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
  // The words the browser matches over the loaded rows: the Memories parser's text, and an
  // `agent:` token as words (a document has no agent).
  const words = `${parsedQuery.text} ${parsedQuery.agent === null ? "" : `agent:${parsedQuery.agent}`}`.trim();
  const freeText = useMemo(() => words.toLowerCase().split(/\s+/).filter(Boolean), [words]);
  const visibleRows = useMemo(() => {
    if (!rows) return rows;
    let r = rows;
    // The server already asked for every tag; a build a release behind matches any one.
    const requiredTags = parsedQuery.tags.map((t) => t.toLowerCase());
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
  }, [rows, parsedQuery.tags, freeText, sortBy]);

  // Checkboxes: one row, a Shift-click range, every row shown; Escape clears them while no
  // dialog is up (each owns its Escape).
  const shownRows = firstScreenOnly ? visibleRows?.slice(0, FIRST_SCREEN_ROWS) : visibleRows;
  const rowKeys = useMemo(() => (visibleRows ?? []).map((r) => r.source_uri), [visibleRows]);
  const dialogOpen = !!previewEntry || addOpen || !!replaceEntry || !!editEntry;
  const selection = useRowSelection(rowKeys, { enabled: !dialogOpen });
  const { selected } = selection;
  const clearSelection = selection.clear;
  // One tab stop over the rows: arrows and j/k move, Enter previews, Space selects.
  const listNav = useRowNav({ enabled: !dialogOpen && rowKeys.length > 0 });
  const listRef = useCallback(
    (el: HTMLDivElement | null) => {
      listNav(el);
      listBox.current = el;
    },
    [listNav],
  );

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
      reload();
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

  const filtering = listQuery.trim() !== "";
  const failed = !!loadError;
  const docCount = aggregateStats && rows ? `${rows.length.toLocaleString()}${nextCursor ? "+" : ""}` : "";
  // The tags are the server's (they cover the library); the words and a sort but newest
  // first run here, over the rows loaded so far. While the cursor holds more, what they
  // found is said of those rows, never of the library.
  const partial = !!nextCursor && !!rows;
  const loaded = `the first ${(rows?.length ?? 0).toLocaleString()}`;
  const browserOnly = partial && (freeText.length > 0 || sortBy !== "recency");
  // One count, at the end of the toolbar: how many rows are shown (the
  // cursor has no total), the matches for a search or a tag, nothing on an error or before
  // the first read. While a new filter is read the last one stays up.
  const matches = (n: number, more = false) =>
    `${n.toLocaleString()}${more ? "+" : ""} match${n === 1 && !more ? "" : "es"}`;
  const shownCount = visibleRows?.length ?? 0;
  const countText =
    rows === null || failed || emptyLibrary
      ? ""
      : filtering
        ? browserOnly
          ? `${matches(shownCount)} in ${loaded}`
          : matches(shownCount, partial)
        : browserOnly
          ? `The first ${(rows?.length ?? 0).toLocaleString()}, sorted`
          : `${shownCount.toLocaleString()} shown`;
  const reading = rows !== null && rowsFor !== serverTags;
  const lastCount = useRef("");
  if (countText && !reading) lastCount.current = countText;
  const countShown = reading ? lastCount.current : countText;
  const sortSelect = (className: string) => (
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
      className={className}
    >
      {SORTS.map((o) => (
        <Select.Item key={o.value} value={o.value}>
          {o.label}
        </Select.Item>
      ))}
    </Select>
  );
  const toggleRow = selection.toggle;
  const rowActions = useMemo<DocRowActions>(
    () => ({
      open: (r) => void handlers.current.onOpen(r),
      download: (r) => void handlers.current.onDownload(r),
      replace: setReplaceEntry,
      edit: setEditEntry,
      remove: (uri) => void handlers.current.onDelete(uri),
      toggle: toggleRow,
      tag: (t) => setQuery((cur) => toggleQueryToken(cur, `tag:${t}`)),
    }),
    [toggleRow],
  );

  return (
    <PageContainer>
      <PageHeader
        title="Library"
        actions={
          <>
            {/* The empty state's own Add is the one primary while it shows. */}
            {!emptyLibrary && (
              <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
                Add document
              </Button>
            )}
          </>
        }
      >
        {/* The phone's top bar names the namespace; the title row keeps its action beside it. */}
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>
      {/* One line: the library's figures once they are read, what the library is behind the tip. */}
      <div className="mb-4 flex min-h-6 items-center gap-1 text-sm text-text-muted tabular">
        <span className="min-w-0 truncate">
          {docCount && aggregateStats && rows && rows.length > 0
            ? [
                `${docCount} document${rows.length === 1 && !nextCursor ? "" : "s"}`,
                `${aggregateStats.chunks.toLocaleString()} chunk${aggregateStats.chunks === 1 ? "" : "s"}`,
                ...(aggregateStats.withBlob > 0 ? [formatBytesShort(aggregateStats.bytes)] : []),
              ].join(" · ")
            : "Reference docs your agents can read."}
        </span>
        <IconButton
          icon="info-in-circle"
          size="xs"
          label="About the library"
          tooltip="Reference docs your agents can read. Memories are what the agent remembered; the library is what you gave it to read."
        />
      </div>

      {/* The search, the sort and the count, in the one bar that stays at the top as the list
          scrolls, in every state: nothing moves when the rows land. A tag picked from a row
          further down is seen to apply, as its `tag:` in the field. While documents are
          selected, the bar holds their actions. */}
      <ListToolbar
        label="Search and filters"
        count={countShown && <span aria-busy={reading || undefined}>{countShown}</span>}
        selection={
          selected.size > 0
            ? {
                count: selected.size,
                onClear: clearSelection,
                all: { total: rowKeys.length, checked: selection.allSelected, onToggle: selection.toggleAll },
                actions: [{ label: "Delete", icon: "trash", danger: true, loading: bulkBusy, onClick: onBulkDelete }],
              }
            : null
        }
      >
        <ListToolbarSearch>
          <SearchField
            ref={searchRef}
            value={query}
            onChange={(value) => setQuery(value)}
            // A phone's field holds the short form only.
            placeholder={phone ? "Search, or tag:specs" : "Search title, filename, description, or tag — try tag:specs"}
            aria-label="Search library"
          />
        </ListToolbarSearch>
        {queryTags.length > 0 && (
          <Button
            type="empty"
            htmlType="button"
            className="hidden shrink-0 sm:inline-flex"
            onClick={() => {
              // The button leaves with the tags: focus goes to the search.
              searchRef.current?.focus();
              setQuery(words);
            }}
          >
            Clear filters
          </Button>
        )}
        {sortSelect("hidden w-40 shrink-0 sm:block")}
      </ListToolbar>
      {/* A phone's toolbar has room for the search only; the sort sits under it. */}
      <div className="-mt-1 mb-3 flex justify-end sm:hidden">{sortSelect("w-40")}</div>

      {/* Mounted while open, not on every render. */}
      {addOpen && (
        <AddDocumentDialog
          open={addOpen}
          onOpenChange={setAddOpen}
          identity={{ org: identity.org, namespace: identity.namespace }}
          onAdded={reload}
        />
      )}
      {!!replaceEntry && (
        <AddDocumentDialog
          open={!!replaceEntry}
          onOpenChange={(v) => {
            if (!v) setReplaceEntry(null);
          }}
          identity={{ org: identity.org, namespace: identity.namespace }}
          onAdded={reload}
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
        onSaved={reload}
      />

      {rows === null && (
        <LoadingSkeleton label="Loading documents">
          <div className="space-y-2">
            <RowSkeleton kind="document" />
            <RowSkeleton kind="document" />
            <RowSkeleton kind="document" />
          </div>
        </LoadingSkeleton>
      )}

      {failed && <ListState kind="error" things="documents" error={loadError} onRetry={reload} />}

      {emptyLibrary && (
        <ListState
          kind="empty"
          icon="document"
          title="Your library is empty"
          description="Upload PDFs, DOCX, Markdown or any text-shaped file. Your agents can search the extracted text via knowledge.search, and you can download the originals back any time."
          action={{ label: "Add document", onClick: () => setAddOpen(true) }}
        />
      )}

      {rows !== null &&
        !emptyLibrary &&
        !failed &&
        rowsFor === serverTags &&
        tagKey === serverTags &&
        visibleRows?.length === 0 && (
          <ListState
            kind="filtered"
            things="documents"
            filter={listQuery.trim()}
            among={partial ? `${loaded} loaded` : undefined}
            onClear={() => setQuery("")}
          />
        )}

      {rows !== null && rows.length > 0 && shownRows && shownRows.length > 0 && (
        <div ref={listRef} className="space-y-2" aria-busy={reading || undefined}>
          {shownRows.map((r) => (
            <DocRow
              key={r.source_uri}
              entry={r}
              active={previewEntry?.source_uri === r.source_uri}
              checked={selected.has(r.source_uri)}
              selecting={selected.size > 0}
              activeTags={activeTags}
              actions={rowActions}
            />
          ))}
        </div>
      )}

      {nextCursor && !failed && (
        <div className="mt-4">
          <BusyButton type="secondary" htmlType="button" busy={loadingMore} onClick={loadMore}>
            Load more
          </BusyButton>
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
            {previewEntry?.ingested_at && (
              <span>
                Uploaded <ListTime time={previewEntry.ingested_at} quiet />
              </span>
            )}
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
            <TagList tags={previewEntry?.tags || []} max={4} />
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
          {previewLoading && <Loading label="Loading preview" className="absolute inset-0" />}
          {/* Only passive types reach here (previewKind): text as a text node, which nothing
                  parses, and images as <img>. Nothing else is drawn from the file's bytes. */}
          {!previewLoading && previewFile?.url && previewEntry?.has_blob && previewFile.kind === "image" && (
            <div className="absolute inset-0 grid place-items-center p-6 overflow-auto">
              <img
                src={previewFile.url}
                alt={previewEntry.original_filename || "document"}
                className="max-w-full max-h-full object-contain shadow"
              />
            </div>
          )}
          {!previewLoading && previewFile?.text != null && previewEntry?.has_blob && previewFile.kind === "text" && (
            <section
              aria-label={previewEntry.original_filename || "document preview"}
              // biome-ignore lint/a11y/noNoninteractiveTabindex: the file scrolls in this box, and the keyboard scrolls it only once it has focus
              tabIndex={0}
              className="doc-preview-text absolute inset-0 overflow-auto bg-bg"
            >
              {previewFile.size > PREVIEW_TEXT_BYTES && (
                <p className="m-0 px-3 pt-3 text-xs text-text-muted">
                  The first {formatBytesShort(PREVIEW_TEXT_BYTES)}; Download has the whole file.
                </p>
              )}
              <pre className="m-0 p-3 font-mono text-xs text-text whitespace-pre-wrap break-words">
                {previewFile.text}
              </pre>
            </section>
          )}
          {!previewLoading && previewFile?.kind === "download" && previewEntry?.has_blob && (
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
          {!previewLoading && !previewFile && previewEntry && !previewEntry.has_blob && (
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
          {!previewLoading && previewEntry?.has_blob && !previewFile && (
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

/** The More menu's download item, naming what it saves: "Download PDF, 1.8 MB" for an
 *  original file, "Download text (.md)" for a text-only document. */
function downloadLabel(r: KnowledgeEntry): string {
  if (!hasOriginalFile(r)) return "Download text (.md)";
  const ext = /\.([a-z0-9]{1,8})$/i.exec(r.original_filename || "")?.[1];
  const kind = (ext || r.blob_content_type?.split("/")[1]?.split(/[;+.]/)[0] || "").toUpperCase();
  const size = r.blob_size ? formatBytesShort(r.blob_size) : "";
  return ["Download" + (kind ? ` ${kind}` : " original file"), size].filter(Boolean).join(", ");
}

/** Whether a document has an original file to download. False for a
 *  text-only document (no blob, size 0, no filename), whose Download
 * button saves the stored text as Markdown instead. */
function hasOriginalFile(r: KnowledgeEntry): boolean {
  return Boolean(r.has_blob || r.blob_size || r.original_filename);
}

/** What a row asks of the page; one object for the page's life, so a memoised row keeps
 *  its props across a keystroke. */
interface DocRowActions {
  open: (r: KnowledgeEntry) => void;
  download: (r: KnowledgeEntry) => void;
  replace: (r: KnowledgeEntry) => void;
  edit: (r: KnowledgeEntry) => void;
  remove: (sourceUri: string) => void;
  toggle: (sourceUri: string, range?: boolean) => void;
  tag: (tag: string) => void;
}

interface DocRowProps {
  entry: KnowledgeEntry;
  active: boolean;
  checked: boolean;
  /** Some row is checked: a click on the row checks it instead of previewing it. */
  selecting: boolean;
  activeTags: ReadonlySet<string>;
  actions: DocRowActions;
}

// Memoised: a hundred rows of Click UI checkboxes, badges, icons and menus rendered again
// on every keystroke in the search.
const DocRow = memo(function DocRow({ entry: r, active, checked, selecting, activeTags, actions }: DocRowProps) {
  const name = r.title || r.original_filename || r.source_uri;
  const file = r.title ? r.original_filename || r.source_uri : "";
  const menu: RowMenuItem[] = [
    { label: "Preview", icon: "eye", onSelect: () => actions.open(r) },
    { label: downloadLabel(r), icon: "download", onSelect: () => actions.download(r) },
    // Uploading a file into a text-only document is the same replace flow.
    { label: "Replace content", icon: "refresh", onSelect: () => actions.replace(r) },
    { label: "Edit details", icon: "pencil", onSelect: () => actions.edit(r) },
    {
      label: "Copy URI",
      icon: "copy",
      onSelect: () => {
        void navigator.clipboard?.writeText(r.source_uri).then(
          () => toast("Copied", "ok"),
          () => toast("Couldn't copy", "error"),
        );
      },
    },
    { label: "Delete", icon: "trash", danger: true, onSelect: () => actions.remove(r.source_uri) },
  ];
  return (
    <ListRow
      data-doc-row={r.source_uri}
      data-focus-key={`doc:${r.source_uri}`}
      active={active}
      checked={checked}
      trailing={<RowMenu name={name} items={menu} />}
      leading={
        <Checkbox
          checked={checked}
          aria-label={`Select ${name}`}
          onClick={(e) => {
            e.stopPropagation();
            actions.toggle(r.source_uri, e.shiftKey);
          }}
          className="list-row-control"
        />
      }
    >
      <button
        type="button"
        data-row-open=""
        onClick={() => (selecting ? actions.toggle(r.source_uri) : actions.open(r))}
        className="list-row-open block w-full"
        aria-label={`Preview ${name}`}
      >
        {/* Line 1: the document and its state; right, how much is indexed and when. */}
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {/* The icon, the name and its state stay on one line; a long name is cut. */}
          <span className="flex min-w-0 max-w-full items-center gap-2">
            <Icon name="document" size="sm" aria-hidden="true" className="shrink-0" />
            {/* An untitled document is named by its file name or URI, an identifier. */}
            {r.title ? (
              <span className="min-w-0 truncate text-text text-sm font-semibold">{name}</span>
            ) : (
              <code className="min-w-0 truncate text-text font-mono text-xs">{name}</code>
            )}
            {r.superseded && (
              <span className="inline-flex shrink-0">
                <Badge text="Superseded" state="neutral" size="sm" aria-hidden="true" />
                <span className="sr-only">, superseded: a newer version of this URI was uploaded</span>
              </span>
            )}
          </span>
          {/* The meta slot: flush right; under 640 px its own line. */}
          <span className="basis-full text-xs text-text-muted tabular sm:ml-auto sm:basis-auto sm:shrink-0">
            {(r.chunk_count ?? 0).toLocaleString()} chunk{r.chunk_count === 1 ? "" : "s"} ·{" "}
            <ListTime time={r.ingested_at} quiet />
          </span>
        </span>
        {/* Line 2: the file it came from and what it is about. */}
        {(file || r.description) && (
          <span className="mt-1 block truncate text-xs text-text-muted">
            {file && <span className="font-mono">{file}</span>}
            {file && r.description && " · "}
            {r.description}
          </span>
        )}
      </button>
      <TagList tags={r.tags ?? []} activeTags={activeTags} onTagClick={actions.tag} className="mt-2" />
    </ListRow>
  );
});
