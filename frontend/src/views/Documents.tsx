import {
  memo,
  type ReactNode,
  startTransition,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Link } from "@clickhouse/click-ui/Link";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import {
  ApiError,
  listKnowledge,
  deleteKnowledge,
  restoreKnowledge,
  type RestoreRefusal,
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
import { isCollectionName, isMemoryNamespace, kindOf } from "../lib/namespace";
import { collectionTitle } from "../lib/collections";
import { AskAboutThis, exact, quoted } from "../components/AskAboutThis";
import { RowSkeleton } from "../components/Skeleton";
import { committedAnyway, toast, toastError } from "../components/Toast";
import { isPublicationPending } from "../lib/errors";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { ListRow, Loading, LoadingSkeleton, PageHeader, TagList, Tip } from "../components/ui";
import { BusyButton } from "../components/ui/BusyButton";
import { ListState } from "../components/ui/ListState";
import { ListTime } from "../components/ui/ListTime";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { DatasetsDialog } from "../flows/DatasetsDialog";
import { Modal } from "../components/ui/Modal";
import { distinctTitle, repeatedTitles } from "../lib/collections";
import { formatBytesShort } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { AddDocumentDialog } from "../flows/AddDocumentDialog";
import { LibraryCollections, useReadableCollections } from "./LibraryCollections";
import { CollectionsInside } from "./LibraryHierarchy";
import { LibraryPassages, PASSAGES_PANEL_ID } from "./LibraryPassages";
import { EditDocumentMetadataDialog } from "../flows/EditDocumentMetadataDialog";
import { PageContainer } from "../layout/PageContainer";
import { EmptyState } from "../components/EmptyState";
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

/** The collection a Library link names (`?collection=`), or null for the header's namespace. */
function linkedCollection(): string | null {
  const name = new URLSearchParams(window.location.search).get("collection");
  // The page never prints text a link made up: only a collection's name counts.
  return isCollectionName(name) ? name : null;
}

// Why a restore left the document deleted (`knowledge.restore`'s `reason`).
const RESTORE_REFUSED: Record<RestoreRefusal, string> = {
  not_found: "the namespace no longer holds it",
  live: "a live version is already there",
  revision_mismatch: "it changed since it was deleted",
  not_restorable: "an erasure reached it, or no delete retired it",
  expired: "the delete is too old to undo",
};

export function Documents() {
  const picked = useIdentity();
  // The Library reads the header's namespace, or a collection picked here, as the URL names
  // it (`?collection=`): read on every render, so a navigation (a document opened from the
  // search), Back and Forward, and a namespace switch (which drops the key) all show at
  // once. A collection never becomes the header's namespace: that one is also where the
  // person's agents write.
  const [, setPicks] = useState(0);
  useStore((s) => s.navSeq); // a navigation renders the page again
  useEffect(() => {
    const onPop = () => setPicks((n) => n + 1);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const collection = linkedCollection();
  const pickCollection = useCallback((c: string | null) => {
    setViewParam("collection", c);
    // A document open in one place, or a link's still to open, is not in another.
    setViewParam("doc", null);
    useStore.getState().clearPendingDoc();
    setPicks((n) => n + 1);
  }, []);
  // A person's pick is a new history entry, so Back steps through the places they picked;
  // the page's own corrections (leaving a collection it cannot show) replace the entry.
  const goTo = (c: string | null) => {
    if (c === collection) return; // the place already shown: nothing moves
    useStore.getState().clearPendingDoc();
    useStore.getState().navigate("knowledge", { query: c ? { collection: c } : {} });
  };
  // A collection is read only once collection.list names it, so the page never shows or
  // writes one under the namespace's name, nor one the caller cannot open.
  const collections = useReadableCollections(picked.org);
  const shown = collections.rows?.find((c) => c.namespace === collection) ?? null;
  const resolving = !!collection && collections.rows === null && !collections.error;
  // A code or tracker namespace, or none, holds no documents: the Library then reads only a
  // collection picked here.
  const mine = useStore((s) => s.myNamespaces);
  const ownShelf = isMemoryNamespace(picked.namespace, mine);
  const noPlace = !shown && !resolving && !ownShelf;
  // With no namespace in the header, the Library opens the person's own files; a token that
  // is not a person's has none, so it stays on the top level, which lists the collections.
  const opensFirst = picked.namespace ? null : (collections.rows?.find((c) => c.personal) ?? null);
  const fallback = ownShelf
    ? `showing ${picked.namespace}`
    : opensFirst
      ? `showing ${collectionTitle(opensFirst)}`
      : "pick another";
  const createdTold = useRef("");
  // A list that names the linked collection no more leaves it for good; one that failed
  // shows the namespace for now and keeps the link, for "Load collections again".
  useEffect(() => {
    if (!collection || !collections.rows || shown) return;
    // Said, not done silently: a link to one, a revoke while shown, or one just created that
    // a replica does not list yet.
    // "Just created" is said once; a later visit to it is any other link's.
    const created = collections.expected === collection && createdTold.current !== collection;
    createdTold.current = created ? collection : createdTold.current;
    toast(
      created
        ? `${collection} was created but is not listed yet; ${fallback}.`
        : collections.truncated
          ? `${collection} is past the first collections listed here; ${fallback}.`
          : `${collection} is not among the collections you can read now; ${fallback}.`,
      "info",
    );
    pickCollection(null);
  }, [collection, collections.rows, collections.truncated, collections.expected, shown, pickCollection, fallback]);
  // A correction, not a pick: the entry is replaced, so Back leaves the Library.
  useEffect(() => {
    if (!collection && opensFirst) pickCollection(opensFirst.namespace);
  }, [collection, opensFirst, pickCollection]);
  const identity = useMemo(() => (shown ? { ...picked, namespace: shown.namespace } : picked), [picked, shown]);
  const readOnly = shown?.role === "viewer";
  const phone = useMedia(PHONE_QUERY);
  const pendingDocSourceUri = useStore((s) => s.pendingDocSourceUri);
  const clearPendingDoc = useStore((s) => s.clearPendingDoc);
  const [rows, setRows] = useState<KnowledgeEntry[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  // An upload of many files has started: the dialog stays open when the place shown changes.
  const batchStarted = useRef(false);
  // The header's New › Document adds to the header's namespace: it steps aside while a
  // collection is shown (the header never holds one), so a document meant for it is added
  // from this page.
  const elsewhere = resolving || noPlace || !!shown;
  useEffect(() => {
    useStore.getState().setLibraryElsewhere(elsewhere);
  }, [elsewhere]);
  useEffect(() => () => useStore.getState().setLibraryElsewhere(false), []);
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement | null>(null);
  const focusSearchOnRead = useRef(false);
  const [sortBy, setSortBy] = useState<SortBy>("recency");
  const [passageHitsShown, setPassageHitsShown] = useState(false);
  const [passagesOpen, setPassagesOpen] = useState(false);
  // The arrows and j/k move through the list focus was last in: the passages or the documents.
  const [keysIn, setKeysIn] = useState<"documents" | "passages">("documents");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [previewEntry, setPreviewEntry] = useState<KnowledgeEntry | null>(null);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewFile, setPreviewFile] = useState<InlinePreview | null>(null);
  // The preview's Tables dialog: the typed datasets its file became.
  const [tablesFor, setTablesFor] = useState<string | null>(null);
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
  // The namespace the failed read was for: an error belongs to the place it came from.
  const [loadErrorFor, setLoadErrorFor] = useState("");
  // The place ("org/namespace") the rows on screen were read for.
  const [rowsScope, setRowsScope] = useState("");
  // The place whose list returned a document on this visit: a later filtered or failed
  // read there keeps the passages (an unfiltered empty one is an empty library).
  const [listedScope, setListedScope] = useState("");
  // Every reload (Retry, a delete, an add, an edit) goes through the effect below, so an
  // answer for a namespace the page has left is aborted and dropped.
  const [reloads, setReloads] = useState(0);
  const reload = () => setReloads((n) => n + 1);

  const load = (signal: AbortSignal, live: () => boolean) => {
    scopeRef.current += 1;
    if (resolving || noPlace) {
      shownScope.current = "";
      setRows(null);
      setLoadError(null);
      return;
    }
    // Blank only for a new namespace; a new filter keeps the rows until the next land.
    const scope = `${identity.org}/${identity.namespace}`;
    if (shownScope.current !== scope) {
      shownScope.current = scope;
      setRows(null);
      // The last place's failure is not this one's, nor its earlier load on another visit.
      setLoadError(null);
      setListedScope("");
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
          setRowsScope(scope);
          // A read that returned a document: the library holds one, whatever filter
          // narrows a later read. An unfiltered read that returned none: it holds none.
          if (r.rows?.length) setListedScope(scope);
          else if (!tags.length) setListedScope("");
          // A collection that answers again earns a fresh re-read if it is refused later.
          reReadFor.current = "";
          setRowsFor(serverTags);
          setNextCursor(r.next_cursor ?? null);
        }
      })
      .catch((e) => {
        if (!live()) return;
        // A failed read is said as a failure, never as an empty library.
        setLoadError(e);
        setLoadErrorFor(identity.namespace);
        setRows([]);
        setRowsScope(scope);
        setRowsFor(serverTags);
      });
  };

  // A memory write cannot change the document list.
  useAbortableEffect(load, [identity.org, identity.namespace, serverTags, reloads, resolving, noPlace]);
  // A collection refused or gone while shown (deleted, or the grant revoked): reading the
  // collections again leaves it, and the page shows the namespace.
  const refusedShown =
    !!shown &&
    loadErrorFor === shown.namespace &&
    loadError instanceof ApiError &&
    [403, 404].includes(loadError.status);
  // Once per collection: replicas can disagree for a moment (one has the revoke, another
  // not yet), so when the list still names it after a re-read the page leaves it.
  const reloadCollections = collections.reload;
  const reReadFor = useRef("");
  useEffect(() => {
    if (!refusedShown || !shown) return;
    if (reReadFor.current === shown.namespace) {
      // The header's own namespace stays, with its error, until the header picks another.
      if (collection) {
        toast(`${shown.namespace} is not among the collections you can read now; ${fallback}.`, "info");
        pickCollection(null);
      }
      return;
    }
    reReadFor.current = shown.namespace;
    reloadCollections();
  }, [refusedShown, shown, collection, reloadCollections, pickCollection, fallback]);

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
      if (scopeRef.current === scope) toastError("Load more failed", e, { read: true });
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

  // The read behind a `?doc=` link's or a passage's open past the loaded rows. It ends
  // with the page, with its namespace, and when the reader opens or closes a document
  // themselves: its answer then opens nothing.
  const linkRead = useRef<AbortController | null>(null);
  useEffect(() => () => linkRead.current?.abort(), [identity.org, identity.namespace]);

  useEffect(() => {
    // Only rows read for the place shown: a navigation renders the new place before its
    // rows land.
    // Nor while the URL names a collection the list has not confirmed (it may yet).
    if (!pendingDocSourceUri || !rows || rowsScope !== `${identity.org}/${identity.namespace}`) return;
    if (collection && !shown) return;
    const uri = pendingDocSourceUri;
    clearPendingDoc();
    // A link to a document the namespace does not hold opens nothing, and the URL stops
    // naming it; a failed read says so and keeps the link.
    handlers.current.openByUri(uri, () => setViewParam("doc", null));
  }, [pendingDocSourceUri, rows, rowsScope, collection, shown, clearPendingDoc, identity.org, identity.namespace]);
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
          "Its chunks leave search at once. Undo on the notice that follows brings it back; its uploaded file may not come back.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    const place = { org: identity.org, namespace: identity.namespace };
    // Deleted, with only lists and search behind: still a delete, with its Undo.
    let lagging = false;
    try {
      await deleteKnowledge(place, source_uri);
    } catch (e) {
      if (!isPublicationPending(e)) {
        toastError("Delete failed", e);
        return;
      }
      lagging = true;
    }
    // Keyed by the document, so a later delete of it replaces this notice and its Undo.
    toast(
      lagging ? `Deleted ${source_uri}; the list may show it for a moment` : `Deleted ${source_uri}`,
      lagging ? "info" : "ok",
      {
        key: `delete:${place.org}/${place.namespace}/${source_uri}`,
        action: { label: "Undo", run: () => void onRestore(place, source_uri) },
      },
    );
    // The row and its menu leave with the next read: focus then goes to the search.
    focusSearchOnRead.current = true;
    reload();
  };

  // The delete's Undo: the place it was deleted from, whatever the page shows now.
  const onRestore = async (place: { org: string; namespace: string }, source_uri: string) => {
    // The Undo button leaves with its notice: focus goes to the search, or to the page
    // when the notice outlived the Library.
    (searchRef.current ?? document.getElementById("main"))?.focus();
    try {
      const r = await restoreKnowledge(place, source_uri);
      if (r.restored) {
        toast(
          r.original === "gone"
            ? `Restored ${source_uri}; its uploaded original was already removed`
            : `Restored ${source_uri}`,
          "ok",
        );
        reload();
      } else {
        toast(`${source_uri} was not restored: ${RESTORE_REFUSED[r.reason]}`, "info");
      }
    } catch (e) {
      toastError("Restore failed", e);
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
      toastError("Download failed", e, { read: true });
    }
  };

  // Bumped by each open and by leaving the place: what an older open reads arrives too late.
  const openSeq = useRef(0);
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
    // An answer for an open the page has since left (another place shown) is dropped.
    const opened = ++openSeq.current;
    try {
      const docPromise = getKnowledge({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch((e) => {
        if (opened === openSeq.current) toastError("Open failed", e, { read: true });
        return null;
      });
      const blobPromise = r.has_blob
        ? fetchKnowledgePreview({ org: identity.org, namespace: identity.namespace }, r.source_uri).catch(() => null)
        : Promise.resolve(null);
      const [d, blob] = await Promise.all([docPromise, blobPromise]);
      if (opened !== openSeq.current) {
        if (blob?.url) URL.revokeObjectURL(blob.url);
        return;
      }
      setPreviewDoc(d);
      setPreviewFile(blob);
    } finally {
      if (opened === openSeq.current) setPreviewLoading(false);
    }
  };
  // A document by its address: from the rows loaded, else read on its own (a link or a
  // passage past the first page), under one abort so a newer open wins; `onMissing` when
  // the namespace does not hold it.
  const openByUri = (uri: string, onMissing: () => void) => {
    const target = rows?.find((r) => r.source_uri === uri);
    if (target) {
      void onOpen(target);
      return;
    }
    linkRead.current?.abort();
    const ac = new AbortController();
    linkRead.current = ac;
    void findDocument({ org: identity.org, namespace: identity.namespace }, uri, ac.signal)
      .then((found) => {
        if (ac.signal.aborted) return;
        // The latest render's open: its preview state, not the one this read started from.
        if (found) void handlers.current.onOpen(found);
        else onMissing();
      })
      .catch((e) => {
        if (!ac.signal.aborted) toastError("Open failed", e, { read: true });
      });
  };
  // The deep-link effect, the rows and the passage panel call the latest render's handlers
  // without re-firing on them or rendering every row again.
  const handlers = useRef({ onOpen, onDownload, onDelete, openByUri });
  handlers.current = { onOpen, onDownload, onDelete, openByUri };
  // Stable, so the passage list is not built again on each render of the page.
  const openPassage = useCallback(
    (uri: string) => handlers.current.openByUri(uri, () => toast(`${uri} is no longer in this library`, "info")),
    [],
  );

  // Set by "Ask about this", which closes the preview for the assistant panel's field.
  const leavingPreview = useRef(false);
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

  // A preview or a dialog belongs to the place it was opened in: another place shown (Back
  // into a collection, a collection confirmed after a wait) closes it, so nothing opened for
  // one is written to the other. The address is the new place's own.
  const place = `${identity.org}/${identity.namespace}`;
  const openedIn = useRef(place);
  useEffect(() => {
    if (openedIn.current === place) return;
    openedIn.current = place;
    linkRead.current?.abort();
    openSeq.current += 1;
    setPreviewLoading(false);
    // The address stops naming the document the preview showed; a pending one (Back to an
    // entry that names its own) is left to open.
    setPreviewEntry((open) => {
      if (open && new URLSearchParams(window.location.search).get("doc") === open.source_uri) setViewParam("doc", null);
      return null;
    });
    setPreviewDoc(null);
    setPreviewFile((f) => {
      if (f?.url) URL.revokeObjectURL(f.url);
      return null;
    });
    setShowText(false);
    // A batch that has started keeps sending where it began; a dialog with nothing sent closes.
    if (!batchStarted.current) setAddOpen(false);
    setReplaceEntry(null);
    setEditEntry(null);
    setTablesFor(null);
  }, [place]);

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
  const selection = useRowSelection(rowKeys, { enabled: !dialogOpen && !readOnly });
  const { selected } = selection;
  const clearSelection = selection.clear;
  // One tab stop over the rows: arrows and j/k move, Enter previews, Space selects.
  const passagesHaveKeys = passageHitsShown && keysIn === "passages";
  const listNav = useRowNav({ enabled: !dialogOpen && rowKeys.length > 0 && !passagesHaveKeys });
  const listRef = useCallback(
    (el: HTMLDivElement | null) => {
      listNav(el);
      listBox.current = el;
    },
    [listNav],
  );
  const passagesBox = useRef<HTMLDivElement | null>(null);
  // Finding a passage: once the place's list has returned a document on this visit, and
  // through a later filtered or failed read (it searches the whole namespace). A new place
  // folds it.
  const canFindPassages = rows !== null && !emptyLibrary && listedScope === place;
  // Taken away (a new place, or the library emptied), the panel comes back folded.
  useEffect(() => {
    if (!canFindPassages) setPassagesOpen(false);
  }, [canFindPassages]);
  const passagesToggle = (className: string) => (
    <Button
      type="empty"
      htmlType="button"
      iconLeft={passagesOpen ? "chevron-down" : "chevron-right"}
      className={className}
      aria-expanded={passagesOpen}
      aria-controls={passagesOpen ? PASSAGES_PANEL_ID : undefined}
      onClick={() => setPassagesOpen((v) => !v)}
    >
      Find a passage
    </Button>
  );
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      const at = e.target instanceof Node ? e.target : null;
      if (passagesBox.current?.contains(at)) setKeysIn("passages");
      else if (listBox.current?.contains(at)) setKeysIn("documents");
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, []);

  const onBulkDelete = async () => {
    if (selected.size === 0 || bulkBusy) return;
    if (
      !(await confirmAction({
        title: `Delete ${selected.size} document${selected.size === 1 ? "" : "s"}?`,
        message:
          "Their chunks leave search at once. A bulk delete has no Undo here: each document can be restored through the API, though its uploaded file may not come back.",
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
      // Deleted, with only lists and search behind: the selection goes, so none is sent again.
      if (committedAnyway("Bulk delete failed", e)) {
        clearSelection();
        reload();
      }
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
      ...(readOnly
        ? {}
        : { replace: setReplaceEntry, edit: setEditEntry, remove: (uri) => void handlers.current.onDelete(uri) }),
      ...(readOnly ? {} : { toggle: toggleRow }),
      tag: (t) => setQuery((cur) => toggleQueryToken(cur, `tag:${t}`)),
    }),
    [toggleRow, readOnly],
  );

  // Below the documents, so they start where the list rules want them; on a phone the
  // select, a tree, lists the collections inside.
  const inside = !phone && collections.rows && (
    <CollectionsInside
      key={shown?.namespace ?? ""}
      rows={collections.rows}
      truncated={collections.truncated}
      current={shown}
      onPick={goTo}
      className="mt-6"
    />
  );
  const picker = (
    <LibraryCollections
      org={picked.org}
      namespace={ownShelf ? picked.namespace : null}
      collections={collections}
      current={shown}
      opening={resolving ? collection : null}
      place={collection ?? picked.namespace}
      onPick={goTo}
      onLeave={pickCollection}
    />
  );

  // Mounted while open, beside either page, so a batch outlives a page move.
  const addDialog = addOpen && (
    <AddDocumentDialog
      open={addOpen}
      onOpenChange={(v) => {
        if (!v) batchStarted.current = false;
        setAddOpen(v);
      }}
      onBatchStarted={(v) => {
        batchStarted.current = v;
      }}
      identity={{ org: identity.org, namespace: identity.namespace }}
      place={shown ? distinctTitle(shown, repeatedTitles(collections.rows ?? [])) : identity.namespace}
      onAdded={reload}
    />
  );
  // The dialog keeps one place in the tree whichever page shows, so React keeps it.
  const withDialog = (page: ReactNode) => (
    <>
      {page}
      {addDialog}
    </>
  );

  if (noPlace) {
    // Where this namespace's data lives instead, as the memory views' empty page says.
    const kind = kindOf(picked.namespace, mine);
    const go = useStore.getState();
    // The list's answer decides: none while it is read, and a failure is not "none".
    const listed = collections.rows;
    // Nothing said while the list is read, or while one's own files open by themselves.
    const unknown = !picked.namespace && ((listed === null && !collections.error) || !!opensFirst);
    // No namespace, but collections to pick: the page asks only for the pick.
    const picking = !picked.namespace && !!listed?.length;
    const action = picking
      ? undefined
      : !picked.namespace
        ? { label: "Browse and join spaces", onClick: () => go.setView("browse") }
        : kind === "qa" || kind === "tracker"
          ? { label: "Open in Patterns", onClick: () => go.navigate("graph", { query: { mode: "patterns" } }) }
          : kind === "code"
            ? { label: "Open repositories", onClick: () => go.setView("code") }
            : undefined;
    const pick = listed !== null && listed.length === 0 ? "Create a collection above" : "Pick a collection above";
    const where = picked.namespace
      ? `${picked.namespace} holds no documents. `
      : picking
        ? "Collections are shared document libraries. "
        : "Your token reaches no namespace yet. ";
    const page = (
      <PageContainer>
        <PageHeader title="Library">
          <ScopeBadge kind="namespace" value={picked.namespace} />
        </PageHeader>
        {picker}
        {/* Not known yet: nothing said, so nothing is taken back. */}
        {!unknown && (
          <EmptyState
            icon="folder-open"
            title={listed !== null && listed.length === 0 ? "No collection yet" : "Pick a collection"}
            // With no namespace to reach, the namespace menu holds none to offer.
            description={`${where}${pick}${picked.namespace ? ", or a memory namespace from the namespace menu" : ""}.`}
            action={action}
          />
        )}
        {picked.namespace && (
          <p className="text-center text-sm text-text-muted">
            <Link component="button" type="button" onClick={() => go.openNamespacePicker(true)}>
              Open the namespace menu
            </Link>
          </p>
        )}
        {inside}
      </PageContainer>
    );
    return withDialog(page);
  }

  return withDialog(
    <PageContainer>
      <PageHeader
        title="Library"
        actions={
          <>
            {/* The empty state's own Add is the one primary while it shows. */}
            {!emptyLibrary && !readOnly && !resolving && (
              <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
                Add document
              </Button>
            )}
          </>
        }
      >
        {/* The phone's top bar names the namespace; the title row keeps its action beside it. */}
        <span className="hidden sm:contents">
          {/* On a collection the select below names where the documents are. */}
          {!shown && !resolving && <ScopeBadge kind="namespace" value={picked.namespace} />}
        </span>
      </PageHeader>
      {picker}
      {/* One line: the library's figures once they are read, what the library is behind the tip.
          A phone, with the place picker above the list, has no room for it: the bar's count
          says how many are shown. */}
      <div className="mb-4 hidden min-h-6 items-center gap-1 text-sm text-text-muted tabular sm:flex">
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
          selected.size > 0 && !readOnly
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
        {canFindPassages && passagesToggle("hidden shrink-0 sm:inline-flex")}
        {sortSelect("hidden w-40 shrink-0 sm:block")}
      </ListToolbar>
      {/* A phone's toolbar has room for the search only; the passage toggle and the sort sit
          under it. */}
      <div className="-mt-1 mb-3 flex items-center justify-end gap-2 sm:hidden">
        {canFindPassages && passagesToggle("mr-auto")}
        {sortSelect("w-40")}
      </div>

      {/* Inside the documents: a passage, cited by revision and place, opens its document. */}
      {canFindPassages && passagesOpen && (
        <div ref={passagesBox}>
          <LibraryPassages
            identity={identity}
            navEnabled={!dialogOpen && keysIn === "passages"}
            onHitsShown={setPassageHitsShown}
            onOpen={openPassage}
          />
        </div>
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
          title={shown ? "This collection is empty" : "Your library is empty"}
          description={
            readOnly
              ? "Nothing has been added yet. Its members who write can add documents."
              : "Upload a PDF, Word, slide deck, spreadsheet, Markdown or text file: the server extracts it, your agents search it with knowledge.search, and you can download the original back any time."
          }
          action={readOnly ? undefined : { label: "Add document", onClick: () => setAddOpen(true) }}
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

      {inside}

      <Modal
        open={!!previewEntry}
        onOpenChange={(v) => {
          if (!v) closePreview();
        }}
        // "Ask about this" closed it for the assistant panel's field: no focus handed back.
        onCloseAutoFocus={(e) => {
          if (!leavingPreview.current) return;
          leavingPreview.current = false;
          e.preventDefault();
        }}
        title={previewEntry?.title || previewEntry?.original_filename || previewEntry?.source_uri || "Document"}
        className="max-w-[900px] w-full"
        initialFocus="dialog"
      >
        <div className="flex flex-wrap sm:flex-nowrap items-start justify-between gap-4 mb-4">
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
            // On a phone the buttons wrap under the details; wider, they stay at the right.
            <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
              {previewEntry.has_blob && (
                <Button
                  type="secondary"
                  htmlType="button"
                  iconLeft="table"
                  onClick={() => setTablesFor(previewEntry.source_uri)}
                >
                  Tables
                </Button>
              )}
              {!readOnly && (
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
              )}
              <AskAboutThis
                what="document"
                // A collection's document is not in the namespace the answer reads: said by title.
                reason={
                  shown && ownShelf
                    ? `The assistant answers about ${picked.namespace}; this document is in the collection ${collectionTitle(shown)}.`
                    : undefined
                }
                item={{
                  chip: `document ${previewEntry.title || previewEntry.original_filename || previewEntry.source_uri}`,
                  line: `document ${exact(previewEntry.source_uri)}${
                    previewEntry.title ? ` (title ${quoted(previewEntry.title)})` : ""
                  }`,
                }}
                leave={() => {
                  leavingPreview.current = true;
                  closePreview();
                }}
              />
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
                {previewEntry && !readOnly && (
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
      {tablesFor && (
        <DatasetsDialog
          identity={identity}
          sourceUri={tablesFor}
          canApprove={!readOnly}
          onClose={() => setTablesFor(null)}
        />
      )}
    </PageContainer>,
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
  /** The writes; absent where the caller only reads (a collection it views). */
  replace?: (r: KnowledgeEntry) => void;
  edit?: (r: KnowledgeEntry) => void;
  remove?: (sourceUri: string) => void;
  /** Selection, for the bulk delete; absent where the caller only reads. */
  toggle?: (sourceUri: string, range?: boolean) => void;
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
  const { replace, edit, remove, toggle } = actions;
  const menu: RowMenuItem[] = [
    { label: "Preview", icon: "eye", onSelect: () => actions.open(r) },
    { label: downloadLabel(r), icon: "download", onSelect: () => actions.download(r) },
    // Uploading a file into a text-only document is the same replace flow.
    ...(replace ? [{ label: "Replace content", icon: "refresh" as const, onSelect: () => replace(r) }] : []),
    ...(edit ? [{ label: "Edit details", icon: "pencil" as const, onSelect: () => edit(r) }] : []),
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
    ...(remove
      ? [{ label: "Delete", icon: "trash" as const, danger: true, onSelect: () => remove(r.source_uri) }]
      : []),
  ];
  return (
    <ListRow
      data-doc-row={r.source_uri}
      data-focus-key={`doc:${r.source_uri}`}
      active={active}
      checked={checked}
      trailing={<RowMenu name={name} items={menu} />}
      leading={
        toggle && (
          <Checkbox
            checked={checked}
            aria-label={`Select ${name}`}
            onClick={(e) => {
              e.stopPropagation();
              toggle(r.source_uri, e.shiftKey);
            }}
            className="list-row-control"
          />
        )
      }
    >
      <button
        type="button"
        data-row-open=""
        onClick={() => (selecting && toggle ? toggle(r.source_uri) : actions.open(r))}
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
