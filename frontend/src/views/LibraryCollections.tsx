import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { deleteCollection, listCollections, type CollectionEntry } from "../api";
import { RowMenu, type RowMenuItem } from "../components/ui/RowMenu";
import { IconButton } from "../components/IconButton";
import { Popover } from "@clickhouse/click-ui/Popover";
import { PopoverTrigger } from "../components/ui/Menu";
import { confirmAction } from "../components/ConfirmDialog";
import { toast, toastError } from "../components/Toast";
import { CreateCollectionDialog, parentChoices } from "../flows/CreateCollectionDialog";
import { MembersDialog } from "../flows/MembersDialog";
import { humanizeError, truncate } from "../lib/format";
import { useAbortableEffect } from "../lib/hooks";
import { useStore } from "../lib/state";

/** The select's value for the namespace picked in the header: an item cannot carry "". */
const NAMESPACE_SHELF = "__namespace__";

/** Pages of `collection.list` read before the select stops and says so. */
const MAX_PAGES = 20;

const ROLE_LABEL: Record<string, string> = { viewer: "Viewer", writer: "Writer", admin: "Admin" };

/** A description beside a name is cut to this many characters; the name never is. */
const LABEL_TEXT = 80;

/** What a collection is called on screen: its description beside its name, and "My files"
 *  for the user's own (`@me`). */
export function collectionLabel(c: CollectionEntry): string {
  if (c.personal) return "My files";
  const d = truncate(c.description ?? "", LABEL_TEXT);
  return d ? `${c.namespace} · ${d}` : c.namespace;
}

/** The collections in tree order, each parent before its children, with its depth. A
 *  collection whose parent the caller cannot read, or that a cycle in the tree cuts off,
 *  sits at the top. */
export function treeOrder(rows: CollectionEntry[]): { c: CollectionEntry; depth: number }[] {
  const names = new Set(rows.map((c) => c.namespace));
  const children = new Map<string, CollectionEntry[]>();
  for (const c of rows) {
    const parent = c.parent && names.has(c.parent) ? c.parent : "";
    const siblings = children.get(parent);
    if (siblings) siblings.push(c);
    else children.set(parent, [c]);
  }
  // The user's own collection first, then by name.
  const order = (a: CollectionEntry, b: CollectionEntry) =>
    Number(b.personal) - Number(a.personal) || a.namespace.localeCompare(b.namespace);
  const out: { c: CollectionEntry; depth: number }[] = [];
  const seen = new Set<string>();
  const visit = (c: CollectionEntry, depth: number) => {
    if (seen.has(c.namespace)) return;
    seen.add(c.namespace);
    out.push({ c, depth });
    for (const child of [...(children.get(c.namespace) ?? [])].sort(order)) visit(child, depth + 1);
  };
  for (const c of [...(children.get("") ?? [])].sort(order)) visit(c, 0);
  // A cycle reaches no top-level collection: its members start at the top.
  for (const c of [...rows].sort(order)) visit(c, 0);
  return out;
}

export interface ReadableCollections {
  /** Every collection the caller can read, or null while the read is in flight. */
  rows: CollectionEntry[] | null;
  /** More pages than the select reads. */
  truncated: boolean;
  error: unknown;
  /** Read again; `rows` is null until the new answer lands. `expect`: a collection just
   *  created, asked for again a few times while a replica's list has not caught up. */
  reload: (expect?: string) => void;
  /** The collection the last read waited for, or "". */
  expected: string;
}

/** The collections the caller can read in `org` (`collection.list`), every page. */
export function useReadableCollections(org: string): ReadableCollections {
  const [read, setRead] = useState<{ org: string; rows: CollectionEntry[]; truncated: boolean } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [reads, setReads] = useState<{ n: number; expect: string }>({ n: 0, expect: "" });
  const reload = useCallback((expect = "") => {
    setRead(null);
    // An old failure would read as "no collections" before the new answer lands.
    setError(null);
    setReads((r) => ({ n: r.n + 1, expect }));
  }, []);
  useAbortableEffect(
    (signal, live) => {
      setError(null);
      void (async () => {
        for (let attempt = 0; ; attempt++) {
          const rows: CollectionEntry[] = [];
          let cursor: string | null = null;
          for (let page = 0; page < MAX_PAGES; page++) {
            const r = await listCollections(org, { cursor, signal });
            rows.push(...r.collections);
            cursor = r.next_cursor;
            if (!cursor) break;
          }
          const caughtUp = !reads.expect || rows.some((c) => c.namespace === reads.expect);
          if (caughtUp || attempt >= 2) return { rows, truncated: !!cursor };
          await new Promise((r) => setTimeout(r, 1000));
        }
      })()
        .then(({ rows, truncated }) => {
          if (live()) setRead({ org, rows, truncated });
        })
        .catch((e) => {
          if (live()) setError(e);
        });
    },
    [org, reads],
  );
  const current = read && read.org === org ? read : null;
  return { rows: current?.rows ?? null, truncated: current?.truncated ?? false, error, reload, expected: reads.expect };
}

/**
 * Where the Library reads: the namespace picked in the header, or one of the collections
 * the caller can read. A collection is a shelf of documents that only grants open; its
 * menu shares it (Members) and deletes it, and New collection adds one inside it.
 */
export function LibraryCollections({
  org,
  namespace,
  collections,
  current,
  opening = null,
  place = "",
  onPick,
  onDeleted,
  onLeave,
}: {
  org: string;
  /** The header's namespace, or null when it holds no documents (a code or tracker
   *  namespace, or none). */
  namespace: string | null;
  collections: ReadableCollections;
  /** The collection shown, or null for the header's namespace. */
  current: CollectionEntry | null;
  /** A collection the URL names that the list has not confirmed yet. */
  opening?: string | null;
  /** Where the page is, as the URL names it (a collection, else the header's namespace):
   *  the list confirming it is no move. */
  place?: string;
  onPick: (collection: string | null) => void;
  /** A collection deleted from the menu. */
  onDeleted?: (collection: string) => void;
  /** Leave the place shown without a new history entry (it was deleted). */
  onLeave?: (collection: string | null) => void;
}) {
  const [creating, setCreating] = useState(false);
  // The collection Members was opened for: the dialog shows only while that one is shown, so
  // Back to another never lands a grant on it.
  const [members, setMembers] = useState<string | null>(null);
  const { rows, error, reload } = collections;
  // New collection and Members belong to the place they opened in: a move to another
  // closes them, and coming back does not reopen them. The list answering (or read again)
  // moves nothing, so what was typed stays.
  useEffect(() => {
    setCreating(false);
    setMembers(null);
  }, [place]);
  const ordered = useMemo(() => treeOrder(rows ?? []), [rows]);
  const parents = useMemo(() => parentChoices(rows ?? []), [rows]);

  const onDelete = async (c: CollectionEntry) => {
    const ok = await confirmAction({
      title: `Delete ${c.namespace}?`,
      message:
        "Its documents, their original files and its memories are erased from disk, its grants stop applying, and the collections inside it become top-level. The name cannot be used again.",
      confirmLabel: "Delete collection",
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await deleteCollection(org, c.namespace);
      if (r.erasure_status && r.erasure_status !== "completed") {
        toast(`Deleted ${c.namespace}; erasing what it held is being retried`, "info", {
          detail: `erasure ${r.erasure_id}`,
        });
      } else {
        toast(`Deleted ${c.namespace}`, "ok");
      }
      onDeleted?.(c.namespace);
      // Only while it is still the one shown: a Back during the confirm moved elsewhere. A
      // correction, so the entry is replaced: Back does not return to what is gone.
      if (shownRef.current === c.namespace) (onLeave ?? onPick)(null);
      reload();
      // The header's namespace is gone with it: the header picks the next one.
      if (c.namespace === namespace) useStore.getState().openNamespacePicker(true);
    } catch (e) {
      toastError(`Couldn't delete ${c.namespace}`, e);
    }
  };

  // What the place shown is, behind the info button beside the select (and the role, which
  // a phone has no room to print).
  const about = current
    ? current.personal
      ? "Your own files: no one else's grants reach them."
      : `${ROLE_LABEL[current.role] ?? current.role} of a collection: only its members read it${current.parent ? `, with everyone who reaches ${current.parent}` : ""}. Agents find it with knowledge.search.`
    : `${namespace === null ? "" : "The namespace picked at the top. "}A collection is a shelf of documents that only its members read; agents search the ones they can read.`;
  const shownRef = useRef<string | null>(null);
  shownRef.current = current?.namespace ?? null;
  const items: RowMenuItem[] = [];
  if (current && !current.personal) {
    items.push({ label: "Members", icon: "users", onSelect: () => setMembers(current.namespace) });
    if (current.role === "admin") {
      items.push({ label: "Delete collection", icon: "trash", danger: true, onSelect: () => void onDelete(current) });
    }
  }

  return (
    <div className="mb-2 space-y-2 sm:mb-4">
      {/* One row at every width, so the list starts where the list rules want it. */}
      <div className="flex items-center gap-2">
        <Select
          value={opening ? "" : current ? current.namespace : namespace === null ? "" : NAMESPACE_SHELF}
          placeholder={opening ? `Opening ${opening}…` : namespace === null ? "Pick a collection" : undefined}
          onSelect={(v) => onPick(v === NAMESPACE_SHELF ? null : v)}
          triggerProps={{ "aria-label": "Show the documents of" }}
          // A long list is found by typing a name or a description.
          showSearch
          className="min-w-0 flex-1 sm:flex-none sm:w-96"
        >
          {/* A header namespace that is a collection is listed once, as the collection. */}
          {namespace !== null && !ordered.some(({ c }) => c.namespace === namespace) && (
            <Select.Item value={NAMESPACE_SHELF}>{`Namespace ${namespace}`}</Select.Item>
          )}
          {ordered.map(({ c, depth }) => (
            <Select.Item key={c.namespace} value={c.namespace}>
              {/* Indented, not marked: the closed select and a screen reader read the name. */}
              <span className="collection-depth" style={{ "--depth": depth } as CSSProperties}>
                {collectionLabel(c)}
                {/* The whole description, unseen, so the select's search finds a word past the cut. */}
                {c.description && <span hidden>{` ${c.description}`}</span>}
              </span>
            </Select.Item>
          ))}
        </Select>
        {current && (
          <span className="hidden shrink-0 text-sm text-text-muted sm:inline">
            {ROLE_LABEL[current.role] ?? current.role}
          </span>
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
        {items.length > 0 && current && <RowMenu name={current.namespace} items={items} />}
        {/* Its words only where there is room; a phone shows the plus. */}
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
      </div>
      {collections.truncated && (
        <p className="text-xs text-text-muted">
          {`Only the first 10,000 collections are listed here; a link to a later one ${namespace === null ? "asks to pick another" : "shows the namespace"}.`}
        </p>
      )}
      {/* The namespace's documents still show: a note, not a second alert beside the list's. */}
      {!!error && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-danger">
          <span>Couldn't load collections: {humanizeError(error)}</span>
          <Button type="empty" htmlType="button" onClick={() => reload()}>
            Load collections again
          </Button>
        </p>
      )}
      {/* Mounted per open, so it starts on the collection shown. */}
      {creating && (
        <CreateCollectionDialog
          open
          onOpenChange={setCreating}
          org={org}
          parents={parents}
          initialParent={current && parents.includes(current) ? current.namespace : ""}
          onCreated={(name) => {
            // No rows until a read that lists the new collection lands (or gives up), so the
            // pick holds.
            reload(name);
            onPick(name);
          }}
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
