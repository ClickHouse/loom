/** The Library's places as a tree beside the documents (disclosure buttons, plain Tab stops,
 *  no keys of their own), with New collection at its foot. */
import { type CSSProperties, type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { TextField } from "@clickhouse/click-ui/TextField";
import { createCollection, type CollectionEntry } from "../api";
import { toastError } from "../components/Toast";
import { fieldError } from "../components/ui/fieldError";
import { Loading } from "../components/ui/Loading";
import { announceCreated } from "../lib/collectionPlaces";
import { canHold, collectionOrder, collectionTitle, distinctTitle, MAX_TITLE, titleTooLong } from "../lib/collections";
import {
  collectionPath,
  cycleStart,
  isSharedWithMe,
  PlaceTitleLink,
  sharedWithMe,
  topLevelCollections,
} from "./LibraryHierarchy";

/** New collection's `data-focus-key`: the page's move to a collection made here focuses it. */
export const NEW_COLLECTION_FOCUS = "library-new-collection";

/** The collections a person opened in the tree, per org, for this browser. */
function expandedKey(org: string): string {
  return `loom_library_expanded_${org}_v1`;
}

function loadExpanded(org: string): Set<string> {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(expandedKey(org)) || "[]");
    return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set(); // a private window or a blocked store: nothing remembered
  }
}

function saveExpanded(org: string, open: Set<string>): void {
  try {
    localStorage.setItem(expandedKey(org), JSON.stringify([...open]));
  } catch {
    /* quota or storage disabled: the session keeps what is open */
  }
}

/** `set` with `keys` added; itself when it holds them all, so nothing redraws. */
function withAll(set: Set<string>, keys: string[]): Set<string> {
  return keys.every((k) => set.has(k)) ? set : new Set([...set, ...keys]);
}

/** `set` without `key`; itself when it does not hold it. */
function without(set: Set<string>, key: string): Set<string> {
  if (!set.has(key)) return set;
  const next = new Set(set);
  next.delete(key);
  return next;
}

/** Before the first answer the tree has no collections. */
const NONE: CollectionEntry[] = [];

/** The tree's shape: the roots of each section and each collection's children, in order. A
 *  shared collection inside one the caller reads is listed under it, not twice. Collections
 *  no root reaches (a cycle in pages read from replicas that disagree) start at the top, each
 *  listed once. */
export function placeTree(rows: CollectionEntry[]): {
  own: CollectionEntry | null;
  mine: CollectionEntry[];
  shared: CollectionEntry[];
  children: Map<string, CollectionEntry[]>;
} {
  const names = new Set(rows.map((c) => c.namespace));
  const under = new Map<string, CollectionEntry[]>();
  for (const c of rows) {
    if (!c.parent || c.personal) continue;
    const list = under.get(c.parent);
    if (list) list.push(c);
    else under.set(c.parent, [c]);
  }
  for (const list of under.values()) list.sort(collectionOrder);
  // Each collection under the first parent the walk reaches it from.
  const children = new Map<string, CollectionEntry[]>();
  const seen = new Set<string>();
  const visit = (c: CollectionEntry): boolean => {
    if (seen.has(c.namespace)) return false;
    seen.add(c.namespace);
    const kids: CollectionEntry[] = [];
    for (const k of under.get(c.namespace) ?? []) if (visit(k)) kids.push(k);
    if (kids.length > 0) children.set(c.namespace, kids);
    return true;
  };
  const mine = topLevelCollections(rows);
  const shared = sharedWithMe(rows).filter((c) => !c.parent || !names.has(c.parent));
  for (const c of [...mine, ...shared]) visit(c);
  // What is left hangs off a cycle (cycleStart).
  const byName = new Map(rows.map((c) => [c.namespace, c]));
  for (const c of [...rows].sort(collectionOrder)) {
    if (c.personal || seen.has(c.namespace)) continue;
    const top = cycleStart(c, byName);
    if (!visit(top)) continue;
    (isSharedWithMe(top) ? shared : mine).push(top);
  }
  mine.sort(collectionOrder);
  shared.sort(collectionOrder);
  return { own: rows.find((c) => c.personal) ?? null, mine, shared, children };
}

/** The collections above `name` that the list holds. */
function ancestors(byName: Map<string, CollectionEntry>, name: string): string[] {
  const c = byName.get(name);
  if (!c) return [];
  return collectionPath(c, byName)
    .slice(0, -1)
    .flatMap((p) => ("namespace" in p ? [p.namespace] : []));
}

export function LibraryTree({
  org,
  rows,
  repeated,
  byName,
  truncated = false,
  failed = false,
  busy = false,
  viewHeight,
  shelf,
  current,
  onPick,
  onCreated,
}: {
  org: string;
  /** Every collection the caller can read (the last answer, while it is read again), or
   *  null before the first answer. */
  rows: CollectionEntry[] | null;
  /** Titles two collections share (`repeatedTitles`): those show their address too. */
  repeated: Set<string>;
  /** The listed collections by name. */
  byName: Map<string, CollectionEntry>;
  /** The list stopped early: a collection missing from it may still be there. */
  truncated?: boolean;
  /** The list could not be read: the page beside says so and offers to read it again. */
  failed?: boolean;
  /** The list is read again: the tree shows the last answer until the new one lands. */
  busy?: boolean;
  /** The height of <main>'s view (a banner above the page is not taken off). */
  viewHeight: number;
  /** The header's namespace, when it holds documents; null when it does not. */
  shelf: string | null;
  /** The collection shown, or null for the header's namespace. */
  current: string | null;
  onPick: (collection: string | null) => void;
  /** A collection made here, in `org`: read the list again and open it. It can land after
   *  the tree is gone (a resize, or a move to another org). */
  onCreated: (collection: string, org: string) => void;
}) {
  const list = rows ?? NONE;
  // What the person opened, remembered for the next visit.
  const [open, setOpen] = useState<Set<string>>(() => loadExpanded(org));
  // What a move opened to show the place reached: for this visit only.
  const [revealed, setRevealed] = useState<Set<string>>(() => new Set());
  // Pruned once, against the first whole list after mount; later answers, from a lagging
  // replica too, forget no fold.
  const pruned = useRef(false);
  useEffect(() => {
    if (pruned.current || !rows || truncated) return;
    pruned.current = true;
    const names = new Set(rows.map((c) => c.namespace));
    setOpen((prev) => {
      const next = new Set([...prev].filter((n) => names.has(n)));
      return next.size === prev.size ? prev : next;
    });
  }, [rows, truncated]);
  useEffect(() => saveExpanded(org, open), [org, open]);
  // A move opens the place reached and its parents, once the list names it; after that a
  // person can close any of them, the one shown too.
  const shownFor = useRef<string | null>(null);
  useEffect(() => {
    if (shownFor.current === current) return;
    if (current && !byName.has(current)) return;
    shownFor.current = current;
    if (current) setRevealed((prev) => withAll(prev, [...ancestors(byName, current), current]));
  }, [byName, current]);
  const isOpen = (key: string) => open.has(key) || revealed.has(key);
  const toggle = (key: string) => {
    if (isOpen(key)) {
      setOpen((prev) => without(prev, key));
      setRevealed((prev) => without(prev, key));
    } else {
      setOpen((prev) => withAll(prev, [key]));
    }
  };
  const tree = useMemo(() => placeTree(list), [list]);

  // The form's target, fixed when New collection is pressed; null while closed.
  const [making, setMaking] = useState<CollectionEntry | "top" | null>(null);
  const newButton = useRef<HTMLButtonElement | null>(null);
  // The form goes when it is done or dropped: focus goes to the button back in its place.
  const refocus = useRef(false);
  useEffect(() => {
    if (making || !refocus.current) return;
    refocus.current = false;
    newButton.current?.focus();
  }, [making]);
  const stopMaking = () => {
    refocus.current = true;
    setMaking(null);
  };
  const into = making && making !== "top" ? making : null;

  const collection = (c: CollectionEntry, depth: number): ReactNode => {
    const kids = tree.children.get(c.namespace) ?? [];
    const expanded = kids.length > 0 && isOpen(c.namespace);
    return (
      <li key={c.namespace}>
        <PlaceRow
          collection={c.namespace}
          label={distinctTitle(c, repeated)}
          icon={expanded ? "folder-open" : "folder-closed"}
          depth={depth}
          here={c.namespace === current}
          expanded={expanded}
          onToggle={kids.length > 0 ? () => toggle(c.namespace) : undefined}
          onPick={onPick}
        />
        {expanded && <ul>{kids.map((k) => collection(k, depth + 1))}</ul>}
      </li>
    );
  };

  const section = (id: string, label: string, roots: CollectionEntry[]) =>
    roots.length > 0 && (
      <>
        <p id={id} className="section-label library-tree-label">
          {label}
        </p>
        <ul aria-labelledby={id}>{roots.map((c) => collection(c, 0))}</ul>
      </>
    );

  return (
    <nav
      aria-label="Library places"
      aria-busy={busy || undefined}
      className="library-tree"
      style={{ maxHeight: viewHeight }}
    >
      {(shelf !== null || tree.own) && (
        <ul>
          {shelf !== null && (
            <li>
              <PlaceRow
                collection={null}
                label={`Namespace ${shelf}`}
                icon="book"
                depth={0}
                here={current === null}
                onPick={onPick}
              />
            </li>
          )}
          {tree.own && (
            <li>
              <PlaceRow
                collection={tree.own.namespace}
                label={collectionTitle(tree.own)}
                icon="user"
                depth={0}
                here={tree.own.namespace === current}
                onPick={onPick}
              />
            </li>
          )}
        </ul>
      )}
      {/* Below the namespace's row while the list is read; a failed read says so beside the
          page, with a button to read it again. */}
      {rows === null && !failed && <Loading label="Loading collections" layout="inline" className="px-2" />}
      {section("library-tree-mine", "Collections", tree.mine)}
      {section("library-tree-shared", "Shared with you", tree.shared)}
      <div className="library-tree-foot">
        {making ? (
          <NewCollectionForm
            org={org}
            parent={into?.namespace ?? ""}
            label={into ? `New collection in ${distinctTitle(into, repeated)}` : "New collection at the top level"}
            onClose={stopMaking}
            onCreated={onCreated}
          />
        ) : (
          <Button
            ref={newButton}
            type="empty"
            htmlType="button"
            iconLeft="plus"
            data-focus-key={NEW_COLLECTION_FOCUS}
            onClick={() => {
              const here = current ? byName.get(current) : undefined;
              setMaking(here && canHold(here) ? here : "top");
            }}
          >
            New collection
          </Button>
        )}
      </div>
    </nav>
  );
}

/** One place's row: a show/hide button when it holds collections (`onToggle`), then its
 *  link. `collection` is null for the header's namespace. */
function PlaceRow({
  collection,
  label,
  icon,
  depth,
  here,
  expanded,
  onToggle,
  onPick,
}: {
  collection: string | null;
  label: string;
  icon: "book" | "user" | "folder-open" | "folder-closed";
  depth: number;
  here: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  onPick: (collection: string | null) => void;
}) {
  return (
    <div className="library-tree-row" data-selected={here || undefined} style={{ "--depth": depth } as CSSProperties}>
      {onToggle ? (
        <button
          type="button"
          className="library-tree-twist"
          aria-expanded={!!expanded}
          aria-label={`Collections in ${label}`}
          onClick={onToggle}
        >
          <Icon name={expanded ? "chevron-down" : "chevron-right"} size="xs" aria-hidden />
        </button>
      ) : (
        <span className="library-tree-twist" aria-hidden="true" />
      )}
      <PlaceTitleLink
        plain
        collection={collection}
        title={label}
        icon={icon}
        aria-current={here ? "page" : undefined}
        className="library-tree-link"
        onOpen={() => onPick(collection)}
      />
    </div>
  );
}

/** New collection's form at the tree's foot: a title, Create and Cancel. `parent` is "" for
 *  the top level; `onClose` runs after a create and on Cancel. */
function NewCollectionForm({
  org,
  parent,
  label,
  onClose,
  onCreated,
}: {
  org: string;
  parent: string;
  label: string;
  onClose: () => void;
  onCreated: (collection: string, org: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement | null>(null);
  const trimmed = title.trim();
  const tooLong = titleTooLong(trimmed);
  const make = async (e: FormEvent) => {
    e.preventDefault();
    if (!trimmed || tooLong || busy) return;
    setBusy(true);
    try {
      const r = await createCollection(org, "", { parent, title: trimmed, deriveName: true });
      announceCreated(r, trimmed);
      onClose();
      onCreated(r.namespace, org);
    } catch (err) {
      toastError(`Couldn't create ${trimmed}`, err);
      setBusy(false);
      field.current?.focus();
    }
  };
  return (
    <form className="library-tree-form" onSubmit={(e) => void make(e)}>
      <div className="min-w-0 basis-full">
        <TextField
          ref={field}
          id="library-tree-title"
          label={label}
          // The person pressed New collection: the title is what they type next.
          autoFocus
          placeholder="Title"
          value={title}
          // Read-only, not disabled, while it is sent: the focus stays in it.
          readOnly={busy}
          onChange={setTitle}
          {...fieldError("library-tree-title-error", tooLong ? `At most ${MAX_TITLE} characters.` : null)}
        />
      </div>
      <Button type="secondary" htmlType="submit" disabled={!trimmed || tooLong} loading={busy}>
        Create
      </Button>
      <Button type="empty" htmlType="button" disabled={busy} onClick={onClose}>
        Cancel
      </Button>
    </form>
  );
}
