import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { deleteCollection, listCollections, type CollectionEntry } from "../api";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { IconButton } from "../components/IconButton";
import { Popover } from "@clickhouse/click-ui/Popover";
import { PopoverTrigger } from "../components/ui/Menu";
import { confirmAction } from "../components/ConfirmDialog";
import { toast, toastError } from "../components/Toast";
import { type ClaimCreateVersion, CreateCollectionDialog } from "../flows/CreateCollectionDialog";
import { MoveCollectionDialog } from "../flows/MoveCollectionDialog";
import { MembersDialog } from "../flows/MembersDialog";
import { CollectionBack, CollectionChips, CollectionPath, cycleStart } from "./LibraryHierarchy";
import {
  COLLECTIONS_LISTED,
  COLLECTIONS_PAGES,
  canHold,
  collectionOrder,
  collectionTitle,
  distinctTitle,
  namespaceLabel,
  roleLabel,
} from "../lib/collections";
import { fmtInt } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { wait } from "../lib/wait";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { PLACE_PICKER_LABEL } from "../lib/useTreeFocusHandoff";
import { invalidateSharedCollections, onCollectionsChanged } from "../lib/collectionPlaces";

/** The select's value for the namespace picked in the header: an item cannot carry "". */
const NAMESPACE_SHELF = "__namespace__";

/** A description beside a name is cut to this many characters; the name never is. */
const LABEL_TEXT = 80;

/** What a collection is called on screen: its title (its name when it has none, and both
 *  when another collection shows the same title) with its description beside it, and
 *  "My files" for the user's own (`@me`). */
export function collectionLabel(c: CollectionEntry, repeated: Set<string> = new Set()): string {
  if (c.personal) return "My files";
  const shown = distinctTitle(c, repeated);
  // By character as a reader sees it, so a cut never splits an emoji.
  // (By code point where the browser has no Intl.Segmenter.)
  const description = c.description ?? "";
  const text =
    typeof Intl.Segmenter === "function"
      ? Array.from(new Intl.Segmenter().segment(description), (s) => s.segment)
      : Array.from(description);
  const d = text.length > LABEL_TEXT ? `${text.slice(0, LABEL_TEXT - 1).join("")}…` : text.join("");
  return d ? `${shown} · ${d}` : shown;
}

/** The collections in tree order, each parent before its children, with its depth. A
 *  collection whose parent the caller cannot read, or that a cycle in the tree cuts off,
 *  sits at the top. */
export function treeOrder(rows: CollectionEntry[]): { c: CollectionEntry; depth: number }[] {
  const byName = new Map(rows.map((c) => [c.namespace, c]));
  const children = new Map<string, CollectionEntry[]>();
  for (const c of rows) {
    const parent = c.parent && byName.has(c.parent) ? c.parent : "";
    const siblings = children.get(parent);
    if (siblings) siblings.push(c);
    else children.set(parent, [c]);
  }
  const out: { c: CollectionEntry; depth: number }[] = [];
  const seen = new Set<string>();
  const visit = (c: CollectionEntry, depth: number) => {
    if (seen.has(c.namespace)) return;
    seen.add(c.namespace);
    out.push({ c, depth });
    for (const child of [...(children.get(c.namespace) ?? [])].sort(collectionOrder)) visit(child, depth + 1);
  };
  for (const c of [...(children.get("") ?? [])].sort(collectionOrder)) visit(c, 0);
  // A cycle reaches no top-level collection: it starts at the top, as the tree's does.
  for (const c of [...rows].sort(collectionOrder)) {
    if (!seen.has(c.namespace)) visit(cycleStart(c, byName), 0);
  }
  return out;
}

export interface ReadableCollections {
  /** Every collection the caller can read, or null while a reload is in flight; a refresh
   *  keeps them. */
  rows: CollectionEntry[] | null;
  /** More pages than the Library reads (`COLLECTIONS_PAGES`). */
  truncated: boolean;
  /** The last answer for this org, kept while it is read again: what stays on screen. Null
   *  before the first answer. */
  kept: { rows: CollectionEntry[]; truncated: boolean } | null;
  /** A read is in flight, of either kind, while an answer for this org is kept (`kept`);
   *  false once it answers or fails. */
  refreshing: boolean;
  error: unknown;
  /** Read again; `rows` is null until the new answer lands. `expect`: a collection just
   *  created, asked for again a few times while a replica's list has not caught up. */
  reload: (expect?: string) => void;
  /** Read again after a failure, waiting for what the failed read waited for: in the
   *  background while an answer is kept, else as a reload. */
  retry: () => void;
  /** The collection the last reload or refresh waited for, or "". */
  expected: string;
}

/** Reads of the list while it leaves out a collection waited for, and the wait between them. */
export const REFRESH_TRIES = 3;
export const REFRESH_WAIT_MS = 1000;

/** The collections the caller can read in `org` (`collection.list`), every page. `reload`
 *  clears `rows` until its answer; a write elsewhere keeps them. `showing`: the collection the
 *  page shows; a refresh that leaves it out reads again, up to `REFRESH_TRIES` times, before
 *  it counts as gone. */
export function useReadableCollections(org: string, showing: string | null = null): ReadableCollections {
  const [answer, setAnswer] = useState<{
    org: string;
    reloads: number;
    refreshes: number;
    rows: CollectionEntry[];
    truncated: boolean;
  } | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Each read waits for `expect` (a name in an org), which the reload or write that asked for
  // it sets; a later reload or write replaces it, a retry keeps it.
  const [asked, setAsked] = useState({ reloads: 0, refreshes: 0, expect: { org: "", name: "" } });
  const reload = useCallback(
    (expect = "") => {
      // An old failure would read as "no collections" before the new answer lands.
      setError(null);
      setAsked((r) => ({ ...r, reloads: r.reloads + 1, expect: { org, name: expect } }));
    },
    [org],
  );
  // Any write to a collection, from a dialog since closed too: read again in the background;
  // one made in another org is not waited for.
  useEffect(
    () =>
      onCollectionsChanged((made) =>
        setAsked((r) => ({
          ...r,
          refreshes: r.refreshes + 1,
          expect: { org: made?.org ?? org, name: made?.name ?? "" },
        })),
      ),
    [org],
  );
  const expected = asked.expect.org === org ? asked.expect.name : "";
  // The collection shown, once the page's last reload is answered and lists it. The read
  // effect captures it when it starts: `showing` changing restarts no read.
  const answered = answer?.org === org && answer.reloads === asked.reloads;
  const hold = answered && showing !== null && answer.rows.some((c) => c.namespace === showing) ? showing : "";
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      void (async () => {
        for (let attempt = 0; ; attempt++) {
          // Pages read from replicas that disagree can list a collection twice: once here.
          const byName = new Map<string, CollectionEntry>();
          let cursor: string | null = null;
          for (let page = 0; page < COLLECTIONS_PAGES; page++) {
            const r = await listCollections(org, { cursor, signal });
            for (const c of r.collections) if (!byName.has(c.namespace)) byName.set(c.namespace, c);
            cursor = r.next_cursor;
            if (!cursor) break;
          }
          const rows = [...byName.values()];
          const caughtUp = [expected, hold].every((name) => !name || byName.has(name));
          if (caughtUp || attempt >= REFRESH_TRIES - 1) return { rows, truncated: !!cursor };
          await wait(REFRESH_WAIT_MS, signal);
        }
      })()
        .then(({ rows, truncated }) => {
          if (live()) setAnswer({ org, reloads: asked.reloads, refreshes: asked.refreshes, rows, truncated });
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [org, asked],
  );
  const kept = answer && answer.org === org ? answer : null;
  const retry = useCallback(() => {
    setError(null);
    setAsked((r) => (kept ? { ...r, refreshes: r.refreshes + 1 } : { ...r, reloads: r.reloads + 1 }));
  }, [kept]);
  const current = kept && kept.reloads === asked.reloads ? kept : null;
  return {
    rows: current?.rows ?? null,
    truncated: current?.truncated ?? false,
    kept,
    refreshing: !!kept && !error && (!current || current.refreshes !== asked.refreshes),
    error,
    reload,
    retry,
    expected,
  };
}

/** The Library's place line: the place picker (where no tree is shown), the path, the chips
 *  and the collection's menu (Members, Move, Delete). */
export function LibraryCollections({
  org,
  namespace,
  collections,
  repeated,
  byName,
  current,
  opening = null,
  place = "",
  onPick,
  onLeave,
  creating,
  onCreatingChange: setCreating,
  claimCreateVersion,
  onCreated,
  besideTree = false,
}: {
  org: string;
  /** The header's namespace, or null when it holds no documents (a code or tracker
   *  namespace, or none). */
  namespace: string | null;
  collections: ReadableCollections;
  /** Titles two collections share (`repeatedTitles`): those show their address too. */
  repeated: Set<string>;
  /** Every collection the caller can read, by name. */
  byName: Map<string, CollectionEntry>;
  /** The collection shown, or null for the header's namespace. */
  current: CollectionEntry | null;
  /** A collection the URL names that the list has not confirmed yet. */
  opening?: string | null;
  /** Where the page is, as the URL names it (a collection, else the header's namespace):
   *  the list confirming it is no move. */
  place?: string;
  onPick: (collection: string | null) => void;
  /** Leave the place shown without a new history entry (it was deleted). */
  onLeave?: (collection: string | null) => void;
  /** The New collection dialog's open state: the page's, whose empty state opens it too. */
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
  claimCreateVersion: ClaimCreateVersion;
  /** A collection the New collection dialog made (or found by its title), reported only while
   *  that dialog is open: the page opens it unless the person moved or sent another create
   *  since. */
  onCreated: (collection: string, version: number) => void;
  /** The tree beside the page lists the places and makes new ones: the select and its New
   *  collection button step aside, and the path names where the page is. */
  besideTree?: boolean;
}) {
  const [moving, setMoving] = useState<CollectionEntry | null>(null);
  // The collection Members was opened for: the dialog shows only while that one is shown, so
  // Back to another never lands a grant on it.
  const [members, setMembers] = useState<string | null>(null);
  const { rows, error } = collections;
  // New collection and Members belong to the place they opened in: a move to another
  // closes them, and coming back does not reopen them. The list answering (or read again)
  // moves nothing, so what was typed stays.
  useEffect(() => {
    setCreating(false);
    setMembers(null);
    setMoving(null);
  }, [place]);
  // The select's order; beside the tree there is no select.
  const ordered = useMemo(() => (besideTree ? [] : treeOrder(rows ?? [])), [rows, besideTree]);
  // A phone gets the path as one back button in the select's row, and no chips.
  const phone = useMedia(PHONE_QUERY);
  // Above the top of a collection's path: the header's namespace, else the user's own files,
  // the places that list the top-level collections.
  const own = (rows ?? []).find((c) => c.personal);
  const home =
    namespace !== null
      ? { label: namespaceLabel(namespace), collection: null, open: () => onPick(null) }
      : own
        ? { label: collectionTitle(own), collection: own.namespace, open: () => onPick(own.namespace) }
        : undefined;
  const parents = useMemo(() => (rows ?? []).filter(canHold), [rows]);

  const onDelete = async (c: CollectionEntry) => {
    const ok = await confirmAction({
      title: `Delete ${collectionTitle(c)}?`,
      message: `Its documents, their original files and its memories are erased from disk, its grants stop applying, and the collections inside it become top-level. Its address ${c.namespace} is retired for good.`,
      confirmLabel: "Delete collection",
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await deleteCollection(org, c.namespace);
      // The titles and places other pages show come from one shared read.
      invalidateSharedCollections();
      if (r.erasure_status && r.erasure_status !== "completed") {
        toast(`Deleted ${collectionTitle(c)}; erasing what it held is being retried`, "info", {
          detail: `erasure ${r.erasure_id}`,
        });
      } else {
        toast(`Deleted ${collectionTitle(c)}`, "ok");
      }
      // Only while it is still the one shown: a Back during the confirm moved elsewhere. A
      // correction, so the entry is replaced: Back does not return to what is gone.
      if (shownRef.current === c.namespace) (onLeave ?? onPick)(null);
    } catch (e) {
      toastError(`Couldn't delete ${collectionTitle(c)}`, e);
    }
  };

  // A parent the caller reads by its title; one it cannot read by the name the list gives.
  const parentShown = (parent: string) => {
    const p = byName.get(parent);
    return p ? collectionTitle(p) : parent;
  };
  // What the place shown is, behind the info button on the place line (and the role, which
  // a phone has no room to print).
  const about = current
    ? current.personal
      ? "Your own files: no one else's grants reach them."
      : `${roleLabel(current.role)} of a collection: only its members read it${current.parent ? `, with everyone who reaches ${parentShown(current.parent)}` : ""}. Agents search it at its address ${current.namespace}.`
    : `${namespace === null ? "" : "The namespace picked at the top. "}A collection is a shelf of documents that only its members read; agents search the ones they can read.`;
  const shownRef = useRef<string | null>(null);
  shownRef.current = current?.namespace ?? null;
  // My files and the namespace have no path; beside the tree no select names them either, so
  // the line starts with the place's name, not the About button alone.
  const placeName = (): string | null => {
    if (!besideTree) return null;
    if (current?.personal) return collectionTitle(current);
    if (current || opening || namespace === null) return null;
    return namespaceLabel(namespace);
  };
  const lineName = placeName();
  // Whether rows are still drawn (the tree keeps the last answer through a reload too; the
  // select's rows go with it): a failure then is a failed refresh.
  const rowsDrawn = !!(besideTree ? collections.kept : rows);
  // The collection shown, not My files: it has a path, members and a menu.
  const collection = current && !current.personal ? current : null;
  const items: RowMenuItem[] = [];
  if (collection) {
    items.push({ label: "Members", icon: "users", onSelect: () => setMembers(collection.namespace) });
    if (collection.role === "admin") {
      items.push({ label: "Move…", icon: "arrow-right", onSelect: () => setMoving(collection) });
      items.push({
        label: "Delete collection",
        icon: "trash",
        danger: true,
        onSelect: () => void onDelete(collection),
      });
    }
  }

  return (
    <div className="mb-2 space-y-2 sm:mb-4">
      {/* One row at the widths the list budget measures, so the documents start where the list
          rules want them: the path leads it (on a phone, one back button). Narrower, it wraps. */}
      <div className="flex flex-wrap items-center gap-2">
        {!phone && collection && (
          <CollectionPath
            current={collection}
            byName={byName}
            repeated={repeated}
            truncated={collections.truncated}
            home={home}
            onPick={onPick}
            named={besideTree}
          />
        )}
        {lineName && <span className="text-sm">{lineName}</span>}
        {phone && collection && (
          <CollectionBack
            current={collection}
            byName={byName}
            truncated={collections.truncated}
            home={home}
            onPick={onPick}
          />
        )}
        {!besideTree && (
          <Select
            value={opening ? "" : current ? current.namespace : namespace === null ? "" : NAMESPACE_SHELF}
            placeholder={opening ? `Opening ${opening}…` : namespace === null ? "Pick a collection" : undefined}
            onSelect={(v) => onPick(v === NAMESPACE_SHELF ? null : v)}
            triggerProps={{ "aria-label": PLACE_PICKER_LABEL }}
            // A long list is found by typing a name or a description.
            showSearch
            className="min-w-0 flex-1 sm:flex-none sm:w-96"
          >
            {namespace !== null && <Select.Item value={NAMESPACE_SHELF}>{namespaceLabel(namespace)}</Select.Item>}
            {ordered.map(({ c, depth }) => (
              <Select.Item key={c.namespace} value={c.namespace}>
                {/* Indented, not marked: the closed select and a screen reader read the name. */}
                <span className="collection-depth" style={{ "--depth": depth } as CSSProperties}>
                  {collectionLabel(c, repeated)}
                  {/* The name a title hides and the whole description, unseen, so the select's
                      search finds them. */}
                  {(c.title || c.description) && (
                    <span hidden>{` ${c.title ? c.namespace : ""} ${c.description ?? ""}`}</span>
                  )}
                </span>
              </Select.Item>
            ))}
          </Select>
        )}
        {current && (
          <span className="hidden shrink-0 text-sm text-text-muted sm:inline">{roleLabel(current.role)}</span>
        )}
        {/* A popover, not a tooltip: a tap on a phone opens it, where the role is not printed. */}
        <Popover>
          <PopoverTrigger>
            <IconButton icon="info-in-circle" size="xs" label="About this place" />
          </PopoverTrigger>
          <Popover.Content align="end" className="w-[min(320px,90vw)] text-sm">
            {about}
          </Popover.Content>
        </Popover>
        {collection && <RowMenu name={collectionTitle(collection)} items={items} />}
        {/* The collections inside, after the path; a phone's select lists them. */}
        {!phone && (
          // One per place, so Show more starts again in the next.
          <CollectionChips
            key={current?.namespace ?? ""}
            rows={rows}
            repeated={repeated}
            collection={collection}
            onPick={onPick}
          />
        )}
        {/* Its words only where there is room; a phone shows the plus. */}
        {!besideTree && (
          <Button
            type="secondary"
            htmlType="button"
            iconLeft="plus"
            aria-label="New collection"
            className="shrink-0"
            onClick={() => setCreating(true)}
          >
            <span className="hidden sm:inline">New collection</span>
          </Button>
        )}
      </div>
      {collections.truncated && (
        <p className="text-xs text-text-muted">
          {`Only the first ${fmtInt(COLLECTIONS_LISTED)} collections are listed here; a link to a later one does not open it.`}
        </p>
      )}
      {/* The namespace's documents still show: a note, not a second alert beside the list's. */}
      {!!error && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-danger">
          <span>
            {rowsDrawn ? "Couldn't refresh collections" : "Couldn't load collections"}:{" "}
            {humanizeError(error, { bare: true, read: true })}
          </span>
          {/* With rows drawn it tries again in place: a reload would blank them and the documents. */}
          <Button type="empty" htmlType="button" onClick={collections.retry}>
            Load collections again
          </Button>
        </p>
      )}
      {moving && (
        <MoveCollectionDialog
          org={org}
          collection={moving}
          parents={parents}
          all={rows ?? []}
          onClose={() => setMoving(null)}
        />
      )}
      {/* Mounted per open, so it starts on the collection shown. */}
      {creating && (
        <CreateCollectionDialog
          open
          onOpenChange={setCreating}
          org={org}
          parents={parents}
          headerNamespace={namespace}
          initialParent={current && parents.includes(current) ? current.namespace : ""}
          claimCreateVersion={claimCreateVersion}
          onCreated={onCreated}
        />
      )}
      {members && current && members === current.namespace && (
        <MembersDialog
          open
          onOpenChange={(v) => {
            if (!v) setMembers(null);
          }}
          org={org}
          namespace={current.namespace}
          admin={current.role === "admin"}
        />
      )}
    </div>
  );
}
