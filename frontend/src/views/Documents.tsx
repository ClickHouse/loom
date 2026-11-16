import {
  type DragEvent,
  lazy,
  memo,
  type ReactNode,
  startTransition,
  Suspense,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
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
  type CollectionEntry,
} from "../api";
import { setViewParam, useStore, useIdentity } from "../lib/state";
import { invalidateSharedCollections } from "../lib/collectionPlaces";
import { isCollectionName, isMemoryNamespace, kindOf } from "../lib/namespace";
import { canCall } from "../lib/canCall";
import { AskAboutThis, exact, quoted } from "../components/AskAboutThis";
import { RowSkeleton } from "../components/Skeleton";
import { toast, toastError, toastWithId } from "../components/Toast";
import { committedAnyway, errorLine, toastCommitted } from "../lib/errorWords";
import { isPublicationPending } from "../lib/errors";
import { confirmAction } from "../components/ConfirmDialog";
import { IconButton } from "../components/IconButton";
import { ListRow, Loading, LoadingSkeleton, PageHeader, TagList, Tip, Unavailable } from "../components/ui";
import { needsRole, useNamespaceRoleRead } from "../lib/namespaceRole";
import { ROLE_LOADING, needsLine } from "../lib/callReason";
import { BusyButton } from "../components/ui/BusyButton";
import { ListState } from "../components/ui/ListState";
import { DropZone } from "../components/ui/DropZone";
import { captureFileDrop } from "../lib/folderUpload";
import { ListTime } from "../components/ui/ListTime";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { DatasetsDialog } from "../flows/DatasetsDialog";
import { Modal } from "../components/ui/Modal";
import { collectionTitle, distinctTitle, namespaceLabel, repeatedTitles } from "../lib/collections";
import { fmtInt, formatBytesShort } from "../lib/format";
import { useAbortableEffect, useMounted } from "../lib/hooks";
import { ALL_DOCUMENTS, linkedAll, placeQuery } from "../lib/libraryPlace";
import { isRefusal, listAcross, MAX_PLACES_READ, type PlaceFailure } from "../lib/libraryAcross";
import { AddDocumentDialog } from "../flows/AddDocumentDialog";
import { LibraryCollections, treeOrder, useReadableCollections } from "./LibraryCollections";
import { useCollectionSharing } from "./CollectionSharing";
import { MoveDocumentsDialog, type MovePlace } from "../flows/MoveDocumentsDialog";
import { PlaceTitleLink, TopLevelPlaces } from "./LibraryHierarchy";
import { type DocumentDrop, expandedKey, LibraryTree } from "./LibraryTree";
import type { ClaimCreateVersion } from "../flows/CreateCollectionDialog";
import { useTreeHeight } from "../lib/useTreeHeight";
import { LibraryPassages, PASSAGES_PANEL_ID } from "./LibraryPassages";
import { EditDocumentMetadataDialog } from "../flows/EditDocumentMetadataDialog";
import { PageContainer } from "../layout/PageContainer";
import { EmptyState } from "../components/EmptyState";
import { KnowledgeGaps } from "./KnowledgeGaps";
import { useAcrossRead } from "./useAcrossRead";
import { ScopeBadge } from "../components/ScopeBadge";
import { oneOf } from "../lib/oneOf";
import { parseSearchQuery, toggleQueryToken } from "../lib/memoriesSearch";
import { useMedia, PHONE_QUERY } from "../lib/useMedia";
import { useDragHold } from "../lib/useDragHold";
import { useRowNav } from "../lib/useRowNav";
import { useLastPageFocus } from "../lib/useLastPageFocus";
import { useRowSelection } from "../lib/useRowSelection";
import { useTreeFocusHandoff } from "../lib/useTreeFocusHandoff";
import { focusViewHeading } from "../lib/place";

// Loaded with the Library's place view; SourcesList decides whether its button shows.
const SourcesList = lazy(() => import("./SourcesList").then((m) => ({ default: m.SourcesList })));

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

/** What the browser tab names: the collection shown, else the namespace shown (`shelf`), else
 *  nothing (a linked collection still read). My files without its address, even when another
 *  collection is titled so, as the tree's row shows it. */
function placeTitle(shown: CollectionEntry | null, repeated: Set<string>, shelf: string | null): string | undefined {
  if (shown) return shown.personal ? collectionTitle(shown) : distinctTitle(shown, repeated);
  if (shelf !== null) return namespaceLabel(shelf);
  return undefined;
}

/** The collection a Library link names (`?collection=`), or null for the header's namespace. */
function linkedCollection(): string | null {
  const name = new URLSearchParams(window.location.search).get("collection");
  // The page never prints text a link made up: only a collection's name counts.
  return isCollectionName(name) ? name : null;
}

/** A move is a new history entry, so Back steps through the places picked. `arriveFocus` and
 *  `note` are the arrival's focus and words (a tree pick keeps the person on its row). */
function moveTo(c: string | null, arriveFocus?: string, note?: string): void {
  useStore.getState().clearPendingDoc();
  useStore.getState().navigate("knowledge", { query: placeQuery(c), arriveFocus, note });
}

/** A row's key: its address, and in a list of several places its place too, since two
 *  places can hold the same address. */
function rowKeyOf(r: KnowledgeEntry, across: boolean): string {
  return across ? `${r.namespace ?? ""}\n${r.source_uri}` : r.source_uri;
}

/** A search reads the place shown, or every place the person reads. */
type SearchScope = "here" | "everywhere";

/** Why All documents' Add is unavailable when no place it adds to is known. */
const NO_PLACE_TO_ADD = "Open a collection or a memory namespace to add documents there.";
const NO_LIST_TO_ADD =
  "The collections could not be loaded, so My files is not known yet; load them again to add here.";
const ADD_WAITS = "My files is not known until the collections are loaded.";

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
  // All documents (`?place=all`) lists the documents of every place the person reads.
  const all = linkedAll();
  const collection = all ? null : linkedCollection();
  const pickCollection = useCallback((c: string | null) => {
    setViewParam("collection", c);
    setViewParam("place", null);
    // A document open in one place, or a link's still to open, is not in another.
    setViewParam("doc", null);
    useStore.getState().clearPendingDoc();
    setPicks((n) => n + 1);
  }, []);
  // A collection is read only once collection.list names it, so the page never shows or
  // writes one under the namespace's name, nor one the caller cannot open.
  const collections = useReadableCollections(picked.org, collection);
  const { error: listFailed, retry: retryList, reload: reloadCollections } = collections;
  const goTo = useCallback(
    (c: string | null, arriveFocus?: string, note?: string) => {
      // A collection is opened only once the list names it: a list that failed reads again.
      if (c && c !== ALL_DOCUMENTS && listFailed) retryList();
      if (c !== (linkedAll() ? ALL_DOCUMENTS : linkedCollection())) moveTo(c, arriveFocus, note);
    },
    [listFailed, retryList],
  );
  const mounted = useMounted();
  // Bumped whenever the place shown changes (a pick, Back, a correction, another namespace or
  // org), and by each create sent (tree or dialog), so only the latest opens what it made; a
  // failed read leaves it.
  const placeVersion = useRef(0);
  useLayoutEffect(() => {
    placeVersion.current += 1;
  }, [picked.org, picked.namespace, collection, all]);
  const claimCreateVersion = useCallback<ClaimCreateVersion>(() => {
    placeVersion.current += 1;
    return placeVersion.current;
  }, []);
  const openCreated = useCallback(
    (name: string, version: number, arriveFocus?: string) => {
      if (!mounted.current || version !== placeVersion.current) return;
      // The answer named the collection already shown: the create's refresh is enough.
      if (name === linkedCollection()) return;
      // The read starts before the move so the old list never says it is missing.
      flushSync(() => reloadCollections(name));
      moveTo(name, arriveFocus);
    },
    [reloadCollections],
  );
  const shown = collections.rows?.find((c) => c.namespace === collection) ?? null;
  // Read once for every list of places on the page, over the last answer (kept while it is
  // read again): titles two collections share show their address too.
  const repeated = useMemo(() => repeatedTitles(collections.kept?.rows ?? []), [collections.kept]);
  const byName = useMemo(
    () => new Map((collections.kept?.rows ?? []).map((c) => [c.namespace, c])),
    [collections.kept],
  );
  const resolving = !!collection && collections.rows === null && !listFailed;
  // Taken from the kept list, so the header holds steady while the list is read again.
  const headerCollection = collection ? (byName.get(collection) ?? null) : null;
  const treeCurrent = all ? ALL_DOCUMENTS : (shown?.namespace ?? (resolving ? collection : null));
  // A code or tracker namespace, or none, holds no documents: the Library then reads only a
  // collection picked here.
  const mine = useStore((s) => s.myNamespaces);
  const ownShelf = isMemoryNamespace(picked.namespace, mine);
  const noPlace = !all && !shown && !resolving && !ownShelf;
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
    if (!collection && !all && opensFirst) pickCollection(opensFirst.namespace);
  }, [collection, all, opensFirst, pickCollection]);
  // In All documents what is added goes to the header's namespace, else to My files.
  const own = collections.kept?.rows.find((c) => c.personal) ?? null;
  const addsTo = all ? (ownShelf ? null : own) : shown;
  const identity = useMemo(() => (addsTo ? { ...picked, namespace: addsTo.namespace } : picked), [picked, addsTo]);
  // The place a preview or a dialog opened in: another one closes it.
  const place = `${identity.org}/${identity.namespace}${all ? "/all" : ""}`;
  // Each write stays in view for a role known to be below writer, with what it needs: a viewer
  // of the collection shown, or a role on the namespace that falls short of knowledge.add and
  // knowledge.delete (WRITER). While the role loads the writes say so and the selection waits,
  // so nothing flips from available to locked; a role the dashboard cannot know once read
  // (unread, a `*` claim over a lower grant, a scoped token with no row) keeps the writes and
  // the server decides.
  const roleRead = useNamespaceRoleRead(picked.namespace);
  const whoami = useStore((s) => s.whoami);
  // Who the tree remembers folds for. App draws no page before whoami answers (with auth off
  // it answers "local"), so it is "" only for a token that names no subject.
  const subject = whoami?.subject ?? "";
  const viewerOfShown = shown && shown.role === "viewer" ? needsRole("writer", collectionTitle(shown)) : null;
  const roleLoading = !shown && (roleRead.loading || !whoami);
  const viewerHere = roleLoading ? ROLE_LOADING : needsLine("knowledge.delete", roleRead, whoami, picked.namespace);
  // The writes to `identity` (Add, the empty state): the collection shown, My files or the
  // header's namespace; each row's own are `reasonFor`'s.
  // `waits`: not known yet (a role or the collections still loading), rather than refused.
  const addReason = (): { reason: string; waits: boolean } | null => {
    if (addsTo) return addsTo === shown && viewerOfShown ? { reason: viewerOfShown, waits: false } : null;
    if (!all || ownShelf) return viewerHere ? { reason: viewerHere, waits: roleLoading } : null;
    // All documents without the header's namespace adds to My files, once the list names it.
    if (collections.kept) return { reason: NO_PLACE_TO_ADD, waits: false };
    return collections.error ? { reason: NO_LIST_TO_ADD, waits: false } : { reason: ADD_WAITS, waits: true };
  };
  const add = addReason();
  const writeReason = add?.reason ?? null;
  // Known to be unable to add, rather than not known yet: the empty state's words.
  const knownReadOnly = !!add && !add.waits;
  // The place's admins manage its sources. An admin claim reaches no collection, so there
  // only the collection's own role decides.
  const sourcesAdmin = shown ? shown.role === "admin" : canCall("knowledge.source.add", roleRead.role, whoami);
  const [sourcesPanel, setSourcesPanel] = useState<HTMLDivElement | null>(null);
  // The gaps tile lists other people's questions: an admin of the place shown sees it.
  const seesGaps = shown ? shown.role === "admin" : canCall("knowledge.gaps.list", roleRead.role, whoami);
  const phone = useMedia(PHONE_QUERY);
  // The places are a tree beside the documents where <main> has room for both; narrower, or
  // not measured yet, the select stands in.
  const treeHeight = useTreeHeight();
  const besideTree = treeHeight !== null;
  useTreeFocusHandoff(besideTree);
  const pendingDocSourceUri = useStore((s) => s.pendingDocSourceUri);
  const clearPendingDoc = useStore((s) => s.clearPendingDoc);
  const [rows, setRows] = useState<KnowledgeEntry[] | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [initialDrop, setInitialDrop] = useState<ReturnType<typeof captureFileDrop> | undefined>();
  useEffect(() => {
    if (!addOpen) setInitialDrop(undefined);
  }, [addOpen]);
  // New collection: the picker's dialog, which the empty Library opens too.
  const [creatingCollection, setCreatingCollection] = useState(false);
  // An upload of many files has started: the dialog stays open when the place shown changes.
  const batchStarted = useRef(false);
  // The header's New › Document adds to the header's namespace: it steps aside while a
  // collection is shown (the header never holds one), so a document meant for it is added
  // from this page (All documents adds to it only when it holds documents).
  const elsewhere = resolving || noPlace || !!shown || (all && !ownShelf);
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
  // The arrows move through the list focus was last in: the passages or the documents.
  const [keysIn, setKeysIn] = useState<"documents" | "passages">("documents");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [previewEntry, setPreviewEntry] = useState<KnowledgeEntry | null>(null);
  const [previewDoc, setPreviewDoc] = useState<KnowledgeDocument | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewFile, setPreviewFile] = useState<InlinePreview | null>(null);
  // The preview's Tables dialog: the typed datasets its file became.
  const [tablesFor, setTablesFor] = useState<KnowledgeEntry | null>(null);
  const [showText, setShowText] = useState(false);
  const [replaceEntry, setReplaceEntry] = useState<KnowledgeEntry | null>(null);
  // The documents being moved, their source and an optional destination picked by a drop.
  // Showing another place closes the dialog.
  const [moving, setMoving] = useState<{
    from: string;
    uris: string[];
    to?: string;
    action?: "add-to-collection";
  } | null>(null);
  // Whether the open Move to… dialog moved any document: prefer the search when it is mounted.
  const movedSome = useRef(false);
  const moveOpener = useRef<HTMLElement | null>(null);
  const openMove = useCallback(
    (from: string, uris: string[], options?: { to?: string; action?: "add-to-collection" }) => {
      movedSome.current = false;
      const active = document.activeElement;
      moveOpener.current = active instanceof HTMLElement && active !== document.body ? active : null;
      setMoving({ from, uris, ...options });
    },
    [],
  );
  useEffect(() => setMoving(null), [place]);
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
  const filtering = listQuery.trim() !== "";
  // Where a search looks: the place shown, or every place. All documents reads every place
  // whatever is typed; each visit to another place starts here, from its first render.
  const [scopeOf, setScopeOf] = useState<{ at: string; scope: SearchScope }>({ at: place, scope: "here" });
  if (scopeOf.at !== place) setScopeOf({ at: place, scope: "here" });
  const scope = scopeOf.scope;
  const across = all || (scope === "everywhere" && filtering);
  // Every reload (Retry, a delete, an add, an edit) goes through the load effect below, so an
  // answer for a namespace the page has left is aborted and dropped.
  const [reloads, setReloads] = useState(0);
  const reload = () => setReloads((n) => n + 1);
  const reloadAfterWrite = () => {
    reload();
    invalidateSharedCollections();
  };
  const placesWaiting = !collections.kept && !collections.error;
  // The places (`readKey`) the rows on screen were read for.
  const [rowsScope, setRowsScope] = useState("");
  const {
    everyPlace,
    everyRead,
    placesCut,
    readPlaces,
    readKey,
    setReadOf,
    refusals,
    report,
    leftOut,
    placesKnown,
    wholeRead,
    partPlaces,
    passagesIn,
  } = useAcrossRead({
    org: identity.org,
    namespace: identity.namespace,
    shelf: ownShelf ? picked.namespace : null,
    collections,
    across,
    rowsScope,
    reload,
  });
  // A place's name on a row and in the notes: the header's namespace, My files or a
  // collection's title, told apart from another of the same title.
  const placeLabel = useCallback(
    (ns: string) => {
      const c = byName.get(ns);
      if (c) return c.personal ? collectionTitle(c) : distinctTitle(c, repeated);
      return ns === picked.namespace ? namespaceLabel(ns) : ns;
    },
    [byName, repeated, picked.namespace],
  );
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
  // A Load more in flight: a new read stops it.
  const moreRead = useRef<AbortController | null>(null);
  // The place (`place`) whose list returned a document on this visit: a later filtered, failed
  // or rescoped read there keeps the passages (an unfiltered empty one is an empty library).
  const [listedScope, setListedScope] = useState("");
  const listedFor = useRef("");
  const load = (signal: AbortSignal, live: () => boolean) => {
    scopeRef.current += 1;
    moreRead.current?.abort();
    if (resolving || noPlace || (across && placesWaiting)) {
      shownScope.current = "";
      setRows(null);
      setLoadError(null);
      return;
    }
    // Blank only for new places; a new filter keeps the rows until the next land.
    const scope = readKey;
    const places = readPlaces;
    const at = place;
    const under = collections.kept;
    if (shownScope.current !== scope) {
      shownScope.current = scope;
      setRows(null);
      // The last place's failure is not this one's, nor its earlier load on another visit.
      setLoadError(null);
    }
    // Another place starts its passages over; another search scope in this one keeps them.
    if (listedFor.current !== at) {
      listedFor.current = at;
      setListedScope("");
    }
    setNextCursor(null);
    const tags = JSON.parse(serverTags) as string[];
    // No place to read: nothing is asked, and the page says why.
    if (places.length === 0) {
      setRows([]);
      setRowsScope(scope);
      setRowsFor(serverTags);
      setReadOf({ key: scope, read: [], failed: [], oneAtATime: false });
      return;
    }
    listAcross(
      identity.org,
      places,
      { fields: LIBRARY_LIST_FIELDS, limit: LIBRARY_PAGE, ...(tags.length ? { tags } : {}) },
      signal,
      // One place is read whatever was refused: a refusal there is the page's own error.
      across ? refusals.skip : [],
    )
      .then((r) => {
        if (live()) {
          setLoadError(null);
          setRows(r.rows);
          setRowsScope(scope);
          setReadOf({ key: scope, read: r.read, failed: r.failed, oneAtATime: r.oneAtATime });
          // A read of one place says nothing of the others' refusals.
          if (across) refusals.record(under, r);
          // A read that returned a document: the library holds one, whatever filter
          // narrows a later read. An unfiltered read that returned none: it holds none.
          if (r.rows?.length) setListedScope(at);
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
        setLoadErrorFor(places.length === 1 ? places[0] : "");
        setRows([]);
        setRowsScope(scope);
        setRowsFor(serverTags);
        setReadOf(null);
      });
  };

  // A memory write cannot change the document list.
  useAbortableEffect(load, [readKey, serverTags, reloads, resolving, noPlace, across && placesWaiting]);
  // A collection refused or gone while shown (deleted, or the grant revoked): reading the
  // collections again leaves it, and the page shows the namespace.
  const refusedShown =
    !!shown &&
    loadErrorFor === shown.namespace &&
    loadError instanceof ApiError &&
    [403, 404].includes(loadError.status);
  // Once per collection: replicas can disagree for a moment (one has the revoke, another
  // not yet), so when the list still names it after a re-read the page leaves it.
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
  // Where the page is, as the URL names it: a collection, All documents, else the namespace.
  const urlPlace = all ? ALL_DOCUMENTS : (collection ?? picked.namespace);
  const share = useCollectionSharing({
    org: picked.org,
    collection: headerCollection,
    byName,
    repeated,
    truncated: collections.kept?.truncated ?? false,
    // Of the collection's own read: one of every place says nothing of what it holds.
    empty: emptyLibrary && !across,
    place: urlPlace,
    onOpenPlace: goTo,
  });
  const readTags = JSON.parse(serverTags) as string[];
  const loadMore = async () => {
    // The cursor holds for the places its first page read, which the report names.
    if (!nextCursor || loadingMore || !report) return;
    const read = report;
    const scope = scopeRef.current;
    markShown(shownRows?.map((r) => rowKeyOf(r, across)) ?? []);
    setLoadingMore(true);
    const ac = new AbortController();
    moreRead.current = ac;
    try {
      const r = await listAcross(
        identity.org,
        read.read,
        {
          cursor: nextCursor,
          fields: LIBRARY_LIST_FIELDS,
          limit: LIBRARY_PAGE,
          // The tags the list was read with, so the cursor and the filter agree.
          ...(readTags.length ? { tags: readTags } : {}),
        },
        ac.signal,
      );
      if (scopeRef.current !== scope) return;
      setRows((prev) => [...(prev || []), ...r.rows]);
      setNextCursor(r.next_cursor ?? null);
    } catch (e) {
      if (scopeRef.current !== scope || ac.signal.aborted) return;
      // A replica of the other build refuses the cursor this one wrote for a list of several
      // places (a rolling upgrade): the list starts again from its first page, and says so.
      if (e instanceof ApiError && e.code === "VALIDATION_FAILED" && (read.read.length > 1 || read.oneAtATime)) {
        toastWithId(
          "The list was read again from the top: the server that answered could not go on from there",
          "info",
          e,
        );
        reload();
        return;
      }
      toastError("Load more failed", e, { read: true });
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
  useEffect(() => () => linkRead.current?.abort(), [place]);
  // Where a row's document is: the place it was read from, else the place shown.
  const placeOf = (r: KnowledgeEntry) => ({ org: identity.org, namespace: r.namespace || identity.namespace });

  useEffect(() => {
    // Only rows read for the place shown: a navigation renders the new place before its
    // rows land.
    // Nor while the URL names a collection the list has not confirmed (it may yet).
    if (!pendingDocSourceUri || !rows || rowsScope !== readKey) return;
    if (collection && !shown) return;
    const uri = pendingDocSourceUri;
    clearPendingDoc();
    // A link to a document the namespace does not hold opens nothing, and the URL stops
    // naming it; a failed read says so and keeps the link.
    handlers.current.openByUri(uri, () => setViewParam("doc", null));
  }, [pendingDocSourceUri, rows, rowsScope, collection, shown, clearPendingDoc, readKey]);
  // After a row's delete, the read that drops the row hands focus to the search field (a
  // move's dialog hands it there itself as it closes).
  useEffect(() => {
    if (!focusSearchOnRead.current) return;
    focusSearchOnRead.current = false;
    searchRef.current?.focus();
  }, [rows]);
  const onDelete = async (r: KnowledgeEntry) => {
    const source_uri = r.source_uri;
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
    const place = placeOf(r);
    try {
      await deleteKnowledge(place, source_uri);
      toast(`Deleted ${source_uri}`, "ok", undoNotice(place, source_uri));
    } catch (e) {
      if (!isPublicationPending(e)) {
        toastError("Delete failed", e);
        return;
      }
      // Deleted, with only lists and search behind: still a delete, with its Undo.
      toastCommitted(
        e,
        `Deleted ${source_uri}; the list may show it for a moment, and Undo may need a moment too`,
        undoNotice(place, source_uri),
      );
    }
    // The row and its menu leave with the next read: focus then goes to the search.
    focusSearchOnRead.current = true;
    reloadAfterWrite();
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
        reloadAfterWrite();
      } else {
        toast(`${source_uri} was not restored: ${RESTORE_REFUSED[r.reason]}`, "info");
      }
    } catch (e) {
      // Refused for now (another write holds the source, or a delivery may still land, which
      // can wait on an operator): the notice comes back with its Undo, which left when pressed.
      if (e instanceof ApiError && e.code === "BACKEND_UNAVAILABLE") {
        toastWithId(
          `${source_uri} is not restored yet; try Undo again later`,
          "info",
          e,
          undoNotice(place, source_uri),
        );
        return;
      }
      toastError("Restore failed", e);
    }
  };
  // A delete's notice options: its key (a later delete of the document replaces it) and Undo.
  const undoNotice = (place: { org: string; namespace: string }, source_uri: string) => ({
    key: `delete:${place.org}/${place.namespace}/${source_uri}`,
    action: { label: "Undo", run: () => void onRestore(place, source_uri) },
  });

  const onDownload = async (r: KnowledgeEntry) => {
    try {
      await downloadKnowledge(placeOf(r), r.source_uri, r.original_filename || textDownloadName(r.source_uri));
    } catch (e) {
      toastError("Download failed", e, { read: true });
    }
  };

  // Bumped by each open and by leaving the place: what an older open reads arrives too late.
  const openSeq = useRef(0);
  const onOpen = async (r: KnowledgeEntry) => {
    linkRead.current?.abort();
    // The open document is the page's URL (`?doc=`): a reload or Back reopens it. One of
    // another place has no address here: the link would open it in the place shown.
    const at = placeOf(r);
    setViewParam("doc", !all && at.namespace === identity.namespace ? r.source_uri : null);
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
      const docPromise = getKnowledge(at, r.source_uri).catch((e) => {
        if (opened === openSeq.current) toastError("Open failed", e, { read: true });
        return null;
      });
      const blobPromise = r.has_blob
        ? fetchKnowledgePreview(at, r.source_uri).catch(() => null)
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
  // A document by its address and place (the place shown unless given): from the rows
  // loaded, else read on its own (a link or a passage past the first page), under one abort
  // so a newer open wins; `onMissing` when the namespace does not hold it.
  const openByUri = (uri: string, onMissing: () => void, ns = identity.namespace) => {
    const target = rows?.find((r) => r.source_uri === uri && placeOf(r).namespace === ns);
    if (target) {
      void onOpen(target);
      return;
    }
    linkRead.current?.abort();
    const ac = new AbortController();
    linkRead.current = ac;
    void findDocument({ org: identity.org, namespace: ns }, uri, ac.signal)
      .then((found) => {
        if (ac.signal.aborted) return;
        // The latest render's open: its preview state, not the one this read started from.
        if (found) void handlers.current.onOpen({ ...found, namespace: found.namespace || ns });
        else onMissing();
      })
      .catch((e) => {
        if (!ac.signal.aborted) toastError("Open failed", e, { read: true });
      });
  };
  const onMove = (r: KnowledgeEntry, action?: "add-to-collection") =>
    openMove(placeOf(r).namespace, [r.source_uri], { action });
  // Stable, so the passage list is not built again on each render of the page.
  const openPassage = useCallback(
    (uri: string, ns?: string) =>
      handlers.current.openByUri(uri, () => toast(`${uri} is no longer in this library`, "info"), ns),
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
  // Why a row's writes are unavailable: the place shown's reason, or, for a row of another
  // place, that place's role (a viewer of a collection; the server decides the rest).
  const reasonFor = useCallback(
    (r: KnowledgeEntry): string | null => {
      const ns = r.namespace || identity.namespace;
      if (ns === identity.namespace) return writeReason;
      const c = byName.get(ns);
      if (c) return c.role === "viewer" ? needsRole("writer", collectionTitle(c)) : null;
      // With a collection shown, `viewerHere` does not wait for the namespace's role: this does.
      if (ns === picked.namespace) return roleRead.loading || !whoami ? ROLE_LOADING : viewerHere;
      return null;
    },
    [identity.namespace, writeReason, byName, picked.namespace, roleRead.loading, whoami, viewerHere],
  );
  // Only rows whose place takes the writes can be selected: the selection holds bulk writes.
  const rowKeys = useMemo(
    () => (visibleRows ?? []).filter((r) => reasonFor(r) === null).map((r) => rowKeyOf(r, across)),
    [visibleRows, across, reasonFor],
  );
  // A reason that appears while Edit details is open closes it. Add and Replace stay: they
  // hold an upload in flight, which the server refuses if the role no longer reaches it.
  const editReason = editEntry ? reasonFor(editEntry) : null;
  useEffect(() => {
    if (editReason) setEditEntry(null);
  }, [editReason]);
  // The preview's writes: the previewed document's place's.
  const previewReason = previewEntry ? reasonFor(previewEntry) : writeReason;
  const previewNs = previewEntry ? placeOf(previewEntry).namespace : identity.namespace;
  const previewCollection = byName.get(previewNs);
  const previewIn = previewCollection ? collectionTitle(previewCollection) : previewNs;
  const rowByKey = useMemo(() => new Map((rows ?? []).map((r) => [rowKeyOf(r, across), r])), [rows, across]);
  const dialogOpen = !!previewEntry || addOpen || !!replaceEntry || !!editEntry || !!moving;
  const selection = useRowSelection(rowKeys, { enabled: !dialogOpen && rowKeys.length > 0 });
  const { selected } = selection;
  const clearSelection = selection.clear;
  // One tab stop over the rows: the arrows move, Enter previews, Space selects.
  const passagesHaveKeys = passageHitsShown && keysIn === "passages";
  const listNav = useRowNav({ enabled: !dialogOpen && !!visibleRows?.length && !passagesHaveKeys });
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
  const canFindPassages = !emptyLibrary && listedScope === place;
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
    // One call per place the selected rows are in.
    const groups = selectedByPlace();
    let deleted = 0;
    let answered = false;
    // The keys sent in calls that went through: only those leave the selection.
    const gone: string[] = [];
    try {
      for (const [ns, inPlace] of groups) {
        const uris = inPlace.map((r) => r.source_uri);
        const keys = inPlace.map((r) => rowKeyOf(r, across));
        try {
          const r = await bulkDeleteKnowledge({ org: identity.org, namespace: ns }, uris);
          deleted += r.total_tombstoned;
          answered = true;
          gone.push(...keys);
        } catch (e) {
          // Deleted, with only lists and search behind: the selection goes, so none is sent again.
          const lead = groups.size > 1 ? `Bulk delete failed in ${placeLabel(ns)}` : "Bulk delete failed";
          if (committedAnyway(lead, e)) gone.push(...keys);
        }
      }
      if (answered) {
        toast(`Deleted ${deleted} document${deleted === 1 ? "" : "s"}`, "ok");
      }
      // A place that refused keeps its rows selected; the rest leave the selection.
      selection.drop(gone);
      if (gone.length) reloadAfterWrite();
    } finally {
      setBulkBusy(false);
    }
  };
  // The selected rows, by the place each is in, in the list's order.
  const selectedByPlace = () => {
    const out = new Map<string, KnowledgeEntry[]>();
    for (const key of rowKeys) {
      const r = selected.has(key) ? rowByKey.get(key) : undefined;
      if (!r) continue;
      const ns = placeOf(r).namespace;
      out.set(ns, [...(out.get(ns) ?? []), r]);
    }
    return out;
  };
  // Documents dragged from the list onto the tree: the row, or the selection when the row is in
  // it. A selection across places does not drag, as Move to… takes one place at a time.
  const { held: heldDocs, start: holdDocs, end: endDrag } = useDragHold<{ from: string; uris: string[] }>();
  const startDrag = (r: KnowledgeEntry, key: string, e: DragEvent) => {
    let held = { from: placeOf(r).namespace, uris: [r.source_uri] };
    if (selected.has(key)) {
      const groups = [...selectedByPlace()];
      const only = groups.length === 1 ? groups[0] : undefined;
      if (!only) {
        e.preventDefault();
        return;
      }
      held = { from: only[0], uris: only[1].map((x) => x.source_uri) };
    }
    holdDocs(e, held);
  };
  // The deep-link effect, the rows and the passage panel call the latest render's handlers
  // without re-firing on them or rendering every row again.
  const handlers = useRef({ onOpen, onDownload, onDelete, openByUri, onMove, startDrag });
  handlers.current = { onOpen, onDownload, onDelete, openByUri, onMove, startDrag };

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
      remove: (r) => void handlers.current.onDelete(r),
      move: (r, action) => handlers.current.onMove(r, action),
      toggle: toggleRow,
      tag: (t) => setQuery((cur) => toggleQueryToken(cur, `tag:${t}`)),
      openPlace: (p) => goTo(p),
      dragStart: (r, key, e) => handlers.current.startDrag(r, key, e),
      dragEnd: endDrag,
    }),
    [toggleRow, goTo, endDrag],
  );
  // Each place's line on a row, once per place, so a memoised row keeps its props.
  const whereOf = useMemo(() => {
    const out = new Map<string, DocPlace>();
    for (const ns of everyRead) {
      out.set(ns, { label: placeLabel(ns), place: byName.has(ns) ? ns : null });
    }
    return out;
  }, [everyRead, placeLabel, byName]);
  const selectedGroups = selected.size > 0 ? selectedByPlace() : null;
  const selectedPlaces = selectedGroups ? [...selectedGroups.keys()] : [];
  const moveSelection = (action?: "add-to-collection") => {
    const from = selectedPlaces[0];
    const docs = from ? selectedGroups?.get(from) : undefined;
    if (selectedPlaces.length === 1 && from && docs)
      openMove(
        from,
        docs.map((r) => r.source_uri),
        { action },
      );
  };
  const passagesLeftOut = across && report ? everyPlace.length - report.read.length : 0;
  // All documents with nothing to read: no place listed, or the collections not read.
  const noneToRead = across && rows !== null && readPlaces.length === 0;
  // The search's scope, under the bar while something is searched and there is another place
  // to search: the bar keeps the field's room, and the list starts where it did.
  const scopeLine = !all && filtering && everyPlace.length > 1 && (
    <div className="-mt-1 mb-3 flex items-center gap-2">
      <ButtonGroup
        aria-label="Search in"
        selected={scope}
        onClick={(v) => setScopeOf({ at: place, scope: v === "everywhere" ? "everywhere" : "here" })}
        options={[
          { value: "here", label: shown ? "This collection" : "This namespace" },
          { value: "everywhere", label: "Everywhere" },
        ]}
      />
    </div>
  );
  // What a read of several places left out, said where the rows start: never more than read.
  // One status region for the whole visit of a list of several places, so each note is said.
  const readNotes = across && (
    <div role="status" className="mb-3 space-y-1 text-xs text-text-muted empty:mb-0">
      {report && (
        <ReadNotes
          failed={leftOut}
          firstOnly={report.oneAtATime ? placeLabel(report.read[0] ?? "") : null}
          // The places asked but the one read and those skipped as refused (named apart).
          others={report.oneAtATime ? readPlaces.length - leftOut.length - 1 : 0}
          cut={placesCut}
          collectionsFailed={
            ownShelf && !!collections.error && !collections.kept ? namespaceLabel(picked.namespace) : null
          }
          label={placeLabel}
          onRetry={reload}
        />
      )}
    </div>
  );

  // The top level's collections, below the documents so they start where the list rules
  // want them. Inside a collection the chips on its path's line list them; on a phone the
  // select, a tree, does.
  const topLevel = !phone && !besideTree && collections.rows && (!shown || shown.personal) && (
    <TopLevelPlaces
      key={shown?.namespace ?? ""}
      rows={collections.rows}
      repeated={repeated}
      byName={byName}
      truncated={collections.truncated}
      onPick={goTo}
      className="mt-6"
    />
  );
  const picker = (
    <LibraryCollections
      org={picked.org}
      namespace={ownShelf ? picked.namespace : null}
      collections={collections}
      repeated={repeated}
      byName={byName}
      current={shown}
      // All documents with no place but the collections says their failure once, as its list.
      quietError={all && !ownShelf && !collections.kept}
      opening={resolving ? collection : null}
      place={urlPlace}
      onPick={goTo}
      onLeave={pickCollection}
      creating={creatingCollection}
      onCreatingChange={setCreatingCollection}
      claimCreateVersion={claimCreateVersion}
      onCreated={openCreated}
      besideTree={besideTree}
    />
  );

  // The Library's collections and the one shown, when the list is whole: only then can a
  // folder be placed where the person looks, so the dialog takes one.
  const folderTree =
    collections.rows && !collections.truncated && (!collection || shown)
      ? { rows: collections.rows, shown }
      : undefined;
  // Where documents can go from the place they are in: the header's namespace when the person
  // adds there, and the collections they write, in tree order; not their own place.
  const addsToHeader = ownShelf && canCall("knowledge.add", roleRead.role, whoami);
  const placesFrom = useCallback(
    (from: string): MovePlace[] => {
      const out: MovePlace[] = [];
      if (addsToHeader && from !== picked.namespace) {
        out.push({ namespace: picked.namespace, label: `Namespace ${picked.namespace}`, depth: 0, kind: "namespace" });
      }
      for (const { c, depth } of treeOrder(collections.kept?.rows ?? [])) {
        if (c.role === "viewer" || c.namespace === from) continue;
        out.push({
          namespace: c.namespace,
          label: c.personal ? collectionTitle(c) : distinctTitle(c, repeated),
          depth: c.personal ? 0 : depth,
          kind: c.personal ? "personal" : "collection",
        });
      }
      return out;
    },
    [collections.kept, repeated, addsToHeader, picked.namespace],
  );
  const movingFrom = moving?.from ?? identity.namespace;
  const movePlaces = useMemo(() => placesFrom(movingFrom), [placesFrom, movingFrom]);
  // The tree's places that take the documents held: those Move to… lists for them.
  const documentDrop = useMemo<DocumentDrop | null>(() => {
    if (!heldDocs) return null;
    const { from, uris } = heldDocs;
    return {
      targets: new Set(placesFrom(from).map((p) => p.namespace)),
      onDrop: (to) => {
        endDrag();
        openMove(from, uris, { to });
      },
    };
  }, [heldDocs, placesFrom, endDrag, openMove]);
  const placesTree = treeHeight !== null && (
    <LibraryTree
      // One per person and org: nothing read or opened in one shows in, or is saved for, another.
      key={expandedKey(subject, picked.org)}
      subject={subject}
      org={picked.org}
      rows={collections.kept?.rows ?? null}
      repeated={repeated}
      byName={byName}
      truncated={collections.kept?.truncated}
      viewHeight={treeHeight}
      failed={!!listFailed}
      busy={collections.refreshing}
      namespace={ownShelf ? picked.namespace : null}
      current={treeCurrent}
      onPick={goTo}
      claimCreateVersion={claimCreateVersion}
      onCreated={openCreated}
      documentDrop={documentDrop}
    />
  );
  const moveDialog = moving && (
    <MoveDocumentsDialog
      org={identity.org}
      from={moving.from}
      fromLabel={placeLabel(moving.from)}
      sourceUris={moving.uris}
      action={moving.action}
      places={movePlaces}
      initialTo={moving.to}
      placesReading={
        collections.kept ? (collections.kept.truncated ? "truncated" : null) : collections.error ? "failed" : "loading"
      }
      onMoved={(moved) => {
        // Any answer may have changed the list, a failed call's too.
        if (moved.length) movedSome.current = true;
        reloadAfterWrite();
      }}
      onClose={() => setMoving(null)}
      onCloseAutoFocus={(e) => {
        if (!movedSome.current) return;
        const search = searchRef.current;
        if (!search && moveOpener.current?.isConnected) return;
        // The rows, the menu or the selection bar that opened it may have left.
        e.preventDefault();
        if (search) search.focus();
        else focusViewHeading();
      }}
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
      place={addsTo ? distinctTitle(addsTo, repeated) : identity.namespace}
      tree={folderTree}
      initialDrop={initialDrop}
      // Folders wait for a list still being read (or read again after it failed), or one too
      // long to show; a collection not in a whole list takes none.
      foldersLater={!folderTree && (collections.rows === null || collections.truncated)}
      onAdded={reloadAfterWrite}
    />
  );
  const emptyDropAllowed = !!shown && !across && writeReason === null;
  const emptyAdd = (
    <Unavailable reason={writeReason}>
      <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
        Add document
      </Button>
    </Unavailable>
  );
  // Either page beside the places tree, when there is room for one. The dialog keeps one
  // place in React's tree whichever page shows, so React keeps it.
  const libraryPage = (page: ReactNode) => (
    <>
      <div className={besideTree ? "library-layout" : undefined}>
        {placesTree}
        {page}
      </div>
      {addDialog}
      {moveDialog}
      {share.dialog}
    </>
  );

  if (noPlace) {
    // Where this namespace's data lives instead, as the memory views' empty page says.
    const kind = kindOf(picked.namespace, mine);
    const go = useStore.getState();
    // The list's answer decides: none while it is read, and a failure is not "none".
    const listed = collections.rows;
    // Nothing said while the list is read, or while one's own files open by themselves.
    const unknown = !picked.namespace && ((listed === null && !listFailed) || !!opensFirst);
    // No namespace, but collections to pick: the page asks only for the pick.
    const picking = !picked.namespace && !!listed?.length;
    const action = picking
      ? undefined
      : !picked.namespace
        ? { label: "Find public namespaces to join", onClick: () => go.setView("browse") }
        : kind === "qa" || kind === "tracker"
          ? { label: "Open in Patterns", onClick: () => go.navigate("graph", { query: { mode: "patterns" } }) }
          : kind === "code"
            ? { label: "Open repositories", onClick: () => go.setView("code") }
            : undefined;
    const none = listed !== null && listed.length === 0;
    // Width-neutral: the places are a select above the page, or the tree beside it.
    const pick = none ? "Create a collection" : "Pick a collection";
    const orNamespace = !picked.namespace
      ? ""
      : none
        ? ", or pick a memory namespace from the namespace picker"
        : ", or a memory namespace from the namespace picker";
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
            title={none ? "No collection yet" : "Pick a collection"}
            // With no namespace to reach, the namespace picker holds none to offer.
            description={`${where}${pick}${orNamespace}.`}
            action={none ? { label: "New collection", onClick: () => setCreatingCollection(true) } : action}
            actions={
              none &&
              action && (
                <Button type="secondary" htmlType="button" onClick={action.onClick}>
                  {action.label}
                </Button>
              )
            }
          />
        )}
        {picked.namespace && (
          <p className="text-center text-sm text-text-muted">
            <Link component="button" type="button" onClick={() => go.openNamespacePicker(true)}>
              Open the namespace picker
            </Link>
          </p>
        )}
        {topLevel}
      </PageContainer>
    );
    return libraryPage(page);
  }

  return libraryPage(
    <PageContainer>
      <PageHeader
        title="Library"
        tab={all ? "All documents" : placeTitle(shown, repeated, ownShelf && !resolving ? picked.namespace : null)}
        actions={
          <>
            {/* A place's own sources; All documents is no one place. */}
            {!resolving && !all && !shown?.personal && (
              // Where this place's documents are pulled from; SourcesList decides when its
              // button shows, and opens the list below the header.
              <Suspense fallback={null}>
                <SourcesList
                  key={`${identity.org}/${identity.namespace}`}
                  org={identity.org}
                  namespace={identity.namespace}
                  place={
                    shown
                      ? { label: collectionTitle(shown), noun: "collection" }
                      : { label: identity.namespace, noun: "namespace" }
                  }
                  manageable={sourcesAdmin}
                  section="Sources"
                  panel={sourcesPanel}
                />
              </Suspense>
            )}
            {share.button}
            {/* The empty state's own Add is the one primary while it shows. */}
            {!emptyLibrary && !resolving && (
              <Unavailable reason={writeReason}>
                <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
                  Add document
                </Button>
              </Unavailable>
            )}
          </>
        }
      >
        {/* The phone's top bar names the namespace; the title row keeps its action beside it. */}
        <span className="hidden sm:contents">
          {/* On a collection the select or the path below names where the documents are. */}
          {!shown && !resolving && !all && <ScopeBadge kind="namespace" value={picked.namespace} />}
        </span>
      </PageHeader>
      {picker}
      {seesGaps && !resolving && !all && <KnowledgeGaps org={identity.org} namespace={identity.namespace} />}
      {/* One line: who can see the collection shown, the library's figures once they are read,
          what the library is behind the tip. A phone, with the place picker above the list, has
          room for who can see it only: the bar's count says how many are shown. */}
      <div
        className={`mb-4 min-h-6 flex-wrap items-center gap-x-3 gap-y-1 text-sm text-text-muted tabular ${headerCollection ? "flex" : "hidden sm:flex"}`}
      >
        {share.line}
        <span className={`min-w-0 items-center gap-1 ${headerCollection ? "hidden shrink-0 sm:flex" : "flex"}`}>
          <span className="min-w-0 truncate">
            {docCount && aggregateStats && rows && rows.length > 0
              ? [
                  `${docCount} document${rows.length === 1 && !nextCursor ? "" : "s"}`,
                  `${aggregateStats.chunks.toLocaleString()} chunk${aggregateStats.chunks === 1 ? "" : "s"}`,
                  ...(aggregateStats.withBlob > 0 ? [formatBytesShort(aggregateStats.bytes)] : []),
                  ...(across && report ? [placesRead(report.read.length, everyPlace.length, placesKnown)] : []),
                ].join(" · ")
              : "Reference docs your agents can read."}
          </span>
          <IconButton
            icon="info-in-circle"
            size="xs"
            label="About the library"
            tooltip="Reference docs your agents can read. Memories are what the agent remembered; the library is what you gave it to read."
          />
        </span>
      </div>

      {/* The Sources button opens the place's sources here, so a closed list costs the rows
          no room. */}
      <div ref={setSourcesPanel} />

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
                actions: [
                  {
                    label: "Move to…",
                    icon: "arrow-right",
                    reason: selectedPlaces.length > 1 ? "Move to… takes documents from one place at a time." : null,
                    onClick: () => moveSelection(),
                  },
                  {
                    label: "Add to collection…",
                    icon: "folder-closed",
                    reason:
                      selectedPlaces.length > 1 ? "Add to collection… takes documents from one place at a time." : null,
                    onClick: () => moveSelection("add-to-collection"),
                  },
                  { label: "Delete", icon: "trash", danger: true, loading: bulkBusy, onClick: onBulkDelete },
                ],
              }
            : null
        }
      >
        <ListToolbarSearch>
          <SearchField
            ref={searchRef}
            value={query}
            onChange={(value) => setQuery(value)}
            // The short form on a phone and beside the tree, whose column leaves the field
            // narrow.
            placeholder={
              phone || besideTree ? "Search, or tag:specs" : "Search title, filename, description, or tag (tag:specs)"
            }
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
      {scopeLine}
      {readNotes}

      {/* Inside the documents: a passage, cited by revision and place, opens its document. */}
      {canFindPassages && passagesOpen && (
        <div ref={passagesBox}>
          <LibraryPassages
            identity={{ org: identity.org, namespace: passagesIn?.[0] ?? identity.namespace }}
            namespaces={passagesIn?.slice(1) ?? []}
            waiting={passagesIn === null && !failed}
            unavailable={
              passagesIn === null && failed
                ? "The places to search could not be read; the search runs once the list is read again."
                : null
            }
            notSearched={passagesLeftOut}
            placeLabel={across ? placeLabel : undefined}
            everywhere={across}
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
          identity={placeOf(replaceEntry)}
          onAdded={reloadAfterWrite}
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
        identity={editEntry ? placeOf(editEntry) : { org: identity.org, namespace: identity.namespace }}
        entry={editEntry}
        onSaved={reloadAfterWrite}
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

      {/* All documents with no place to read: the collections' failure, or none at all. */}
      {noneToRead &&
        (collections.error ? (
          <ListState kind="error" things="collections" error={collections.error} onRetry={collections.retry} />
        ) : (
          <ListState
            kind="empty"
            icon="folder-open"
            title="No place to read yet"
            description="Collections you make or are shared with you, and a memory namespace you pick in the header, list their documents here."
            action={{ label: "New collection", onClick: () => setCreatingCollection(true) }}
          />
        ))}

      {emptyLibrary && !noneToRead && (
        <ListState
          kind="empty"
          icon="document"
          title={
            across
              ? wholeRead
                ? "No documents in any place you read"
                : "No documents in the places read"
              : shown
                ? "This collection is empty"
                : "Your library is empty"
          }
          description={
            all && knownReadOnly
              ? "Nothing has been added to the places you read yet. Open a collection you write to add documents there."
              : knownReadOnly
                ? "Nothing has been added yet. Its members who write can add documents."
                : all
                  ? `Upload a PDF, Word, slide deck, spreadsheet, Markdown or text file to ${placeLabel(identity.namespace)}, or open a collection and add it there: the server extracts each, and your agents search them.`
                  : folderTree
                    ? "Upload a PDF, Word, slide deck, spreadsheet, Markdown or text file, or a folder of them: the server extracts each, your agents search them, and you can download the originals back any time."
                    : "Upload a PDF, Word, slide deck, spreadsheet, Markdown or text file: the server extracts it, your agents search it, and you can download the original back any time."
          }
          // One Add, unavailable with why to a role that cannot add or is still loading, so it
          // keeps its focus as the role resolves.
          actions={
            emptyDropAllowed ? (
              <DropZone
                data-testid="library-empty-drop"
                className="space-y-3 p-6"
                onDrop={(e) => {
                  const captured = captureFileDrop(e.dataTransfer);
                  if (!captured.files.length && !captured.entries.length) return;
                  setInitialDrop(captured);
                  setAddOpen(true);
                }}
              >
                <p className="text-sm text-text-muted">
                  {folderTree ? "Drop files or folders here" : "Drop files here"}
                </p>
                {emptyAdd}
              </DropZone>
            ) : (
              emptyAdd
            )
          }
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
            among={
              partial
                ? `${loaded} loaded${partPlaces ? " from the places read" : ""}`
                : partPlaces
                  ? "the places read"
                  : undefined
            }
            onClear={() => setQuery("")}
          />
        )}

      {rows !== null && rows.length > 0 && shownRows && shownRows.length > 0 && (
        <div ref={listRef} className="space-y-2" aria-busy={reading || undefined}>
          {shownRows.map((r) => (
            <DocRow
              key={rowKeyOf(r, across)}
              rowKey={rowKeyOf(r, across)}
              entry={r}
              active={!!previewEntry && rowKeyOf(previewEntry, across) === rowKeyOf(r, across)}
              checked={selected.has(rowKeyOf(r, across))}
              selecting={selected.size > 0}
              activeTags={activeTags}
              actions={rowActions}
              writeReason={reasonFor(r)}
              where={across ? (whereOf.get(placeOf(r).namespace) ?? null) : null}
              draggable={besideTree}
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

      {topLevel}

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
                <Button type="secondary" htmlType="button" iconLeft="table" onClick={() => setTablesFor(previewEntry)}>
                  Tables
                </Button>
              )}
              <Unavailable reason={previewReason} tip="Upload a new file version of this document">
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
              </Unavailable>
              <AskAboutThis
                what="document"
                // A collection's document is not in the namespace the answer reads: said by title.
                reason={
                  ownShelf && previewNs !== picked.namespace
                    ? `Ask AI answers about ${picked.namespace}; this document is in the collection ${previewIn}.`
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
                {previewEntry && (
                  <Unavailable reason={previewReason}>
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
                  </Unavailable>
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
          identity={{ ...identity, ...placeOf(tablesFor) }}
          sourceUri={tablesFor.source_uri}
          approveReason={reasonFor(tablesFor)}
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
  /** The writes. */
  replace: (r: KnowledgeEntry) => void;
  edit: (r: KnowledgeEntry) => void;
  remove: (r: KnowledgeEntry) => void;
  move: (r: KnowledgeEntry, action?: "add-to-collection") => void;
  /** Selection, for the bulk writes, by the row's key; a row whose writes are unavailable has none. */
  toggle: (key: string, range?: boolean) => void;
  tag: (tag: string) => void;
  /** Open a place: a collection, or null for the header's namespace. */
  openPlace: (place: string | null) => void;
  /** A drag of the row (by its key) onto the tree starts, or ends. */
  dragStart: (r: KnowledgeEntry, key: string, e: DragEvent) => void;
  dragEnd: () => void;
}

/** Where a row's document lives, on a list of several places: the place's name and the
 *  collection its link opens (null for the header's namespace). */
interface DocPlace {
  label: string;
  place: string | null;
}

/** "9 places", or "7 of 9 places" when some were not read; "7 places read" when how many
 *  there are is not known (the collections failed, or their list stops early). */
function placesRead(read: number, known: number, whole: boolean): string {
  if (!whole) return `${fmtInt(read)} place${read === 1 ? "" : "s"} read`;
  const n = read === known ? fmtInt(read) : `${fmtInt(read)} of ${fmtInt(known)}`;
  return `${n} place${known === 1 ? "" : "s"}`;
}

/** What a read of several places left out, under the toolbar: places refused or failed, all
 *  but the first on a server that reads one per call, those past one read's most. */
function ReadNotes({
  failed,
  firstOnly,
  others,
  cut,
  collectionsFailed,
  label,
  onRetry,
}: {
  failed: PlaceFailure[];
  /** The one place read, on a server that reads one per call; else null. */
  firstOnly: string | null;
  others: number;
  cut: number;
  /** The one place read when the collections' list failed; else null. */
  collectionsFailed: string | null;
  label: (ns: string) => string;
  onRetry: () => void;
}) {
  const refused = failed.filter((f) => isRefusal(f.error));
  const broken = failed.filter((f) => !refused.includes(f));
  const names = (list: PlaceFailure[]) => list.map((f) => label(f.namespace)).join(", ");
  const lines: string[] = [];
  if (firstOnly !== null) {
    lines.push(
      `Only ${firstOnly} is shown: this server lists one place per read, so ${fmtInt(others)} other place${others === 1 ? " is" : "s are"} not.`,
    );
  }
  if (cut > 0) {
    lines.push(
      `${fmtInt(cut)} place${cut === 1 ? " is" : "s are"} past the most one read takes (${fmtInt(MAX_PLACES_READ)}), and not read.`,
    );
  }
  if (collectionsFailed !== null) {
    lines.push(`Only ${collectionsFailed} is shown: the collections could not be loaded.`);
  }
  if (refused.length)
    lines.push(`Not shown, as you can't read ${refused.length === 1 ? "it" : "them"}: ${names(refused)}.`);
  if (lines.length === 0 && broken.length === 0) return null;
  return (
    <>
      {lines.map((l) => (
        <p key={l}>{l}</p>
      ))}
      {broken.length > 0 && (
        <p className="flex flex-wrap items-center gap-2 text-danger">
          {/* The error's words end the line. */}
          <span>{`Couldn't read ${names(broken)}; the other places are shown: ${errorLine(broken[0].error, { read: true })}`}</span>
          <Button type="empty" htmlType="button" onClick={onRetry}>
            Read again
          </Button>
        </p>
      )}
    </>
  );
}

interface DocRowProps {
  /** The row's key in the list: its address, with its place on a list of several. */
  rowKey: string;
  entry: KnowledgeEntry;
  active: boolean;
  checked: boolean;
  /** Some row is checked: a click on the row checks it instead of previewing it. */
  selecting: boolean;
  activeTags: ReadonlySet<string>;
  actions: DocRowActions;
  /** Why the row's writes are unavailable (its place's role, or still loading), or null; they
   *  stay in the menu with it, and the row has no checkbox. */
  writeReason: string | null;
  /** Where the document lives, on a list of several places. */
  where: DocPlace | null;
  /** The tree is beside the list: a row whose writes are available drags onto it. */
  draggable: boolean;
}

// Memoised: a hundred rows of Click UI checkboxes, badges, icons and menus rendered again
// on every keystroke in the search.
const DocRow = memo(function DocRow({
  rowKey,
  entry: r,
  active,
  checked,
  selecting,
  activeTags,
  actions,
  writeReason,
  where,
  draggable,
}: DocRowProps) {
  const name = r.title || r.original_filename || r.source_uri;
  const file = r.title ? r.original_filename || r.source_uri : "";
  const { replace, edit, remove, move } = actions;
  const toggle = writeReason === null ? actions.toggle : undefined;
  const drags = draggable && writeReason === null;
  const menu: RowMenuItem[] = [
    { label: "Preview", icon: "eye", onSelect: () => actions.open(r) },
    { label: downloadLabel(r), icon: "download", onSelect: () => actions.download(r) },
    // Uploading a file into a text-only document is the same replace flow.
    { label: "Replace content", icon: "refresh", reason: writeReason, onSelect: () => replace(r) },
    { label: "Edit details", icon: "pencil", reason: writeReason, onSelect: () => edit(r) },
    { label: "Move to…", icon: "arrow-right", reason: writeReason, onSelect: () => move(r) },
    {
      label: "Add to collection…",
      icon: "folder-closed",
      reason: writeReason,
      onSelect: () => move(r, "add-to-collection"),
    },
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
    { label: "Delete", icon: "trash", danger: true, reason: writeReason, onSelect: () => remove(r) },
  ];
  return (
    <ListRow
      data-doc-row={rowKey}
      data-focus-key={`doc:${rowKey}`}
      active={active}
      checked={checked}
      draggable={drags || undefined}
      onDragStart={drags ? (e) => actions.dragStart(r, rowKey, e) : undefined}
      onDragEnd={drags ? actions.dragEnd : undefined}
      trailing={<RowMenu name={name} items={menu} />}
      leading={
        toggle && (
          <Checkbox
            checked={checked}
            aria-label={`Select ${name}`}
            onClick={(e) => {
              e.stopPropagation();
              toggle(rowKey, e.shiftKey);
            }}
            className="list-row-control"
          />
        )
      }
    >
      <button
        type="button"
        data-row-open=""
        onClick={() => (selecting && toggle ? toggle(rowKey) : actions.open(r))}
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
      {/* Where it lives, a link to that place, on a list of several places. */}
      {where && (
        <span className="mt-1 flex min-w-0 items-center gap-1 text-xs text-text-muted">
          <span className="shrink-0">In</span>
          <PlaceTitleLink
            collection={where.place}
            title={where.label}
            titleClassName="min-w-0 truncate"
            className="list-row-control doc-row-place min-w-0 max-w-full"
            onOpen={() => actions.openPlace(where.place)}
          />
        </span>
      )}
      <TagList tags={r.tags ?? []} activeTags={activeTags} onTagClick={actions.tag} className="mt-2" />
    </ListRow>
  );
});
