/** The Library's places as a tree beside the documents (disclosure buttons, plain Tab stops,
 *  no keys of their own), with New collection at its foot. */
import { type CSSProperties, type FormEvent, memo, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TextField } from "@clickhouse/click-ui/TextField";
import { type CollectionCreated, createCollection, type CollectionEntry } from "../api";
import { toastError } from "../components/Toast";
import { fieldError } from "../components/ui/fieldError";
import { Loading } from "../components/ui/Loading";
import { IconButton } from "../components/IconButton";
import { announceCreated } from "../flows/CreateCollectionDialog";
import { canHold, collectionOrder, collectionTitle, distinctTitle, MAX_TITLE, titleTooLong } from "../lib/collections";
import {
  collectionPath,
  cycleStart,
  MORE_SHOWN,
  isSharedWithMe,
  PlaceTitleLink,
  sharedWithMe,
  topLevelCollections,
  useShowMore,
} from "./LibraryHierarchy";

/** A tree row's `data-focus-key`: the page's move to a place picked here focuses its row. */
export const placeFocusKey = (collection: string | null) => `library-tree-place-${collection ?? ""}`;

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
  const byName = new Map(rows.map((c) => [c.namespace, c]));
  const under = new Map<string, CollectionEntry[]>();
  for (const c of rows) {
    // My files has a row of its own; collection.move does not refuse it a parent.
    if (!c.parent || c.personal) continue;
    const list = under.get(c.parent);
    if (list) list.push(c);
    else under.set(c.parent, [c]);
  }
  for (const list of under.values()) list.sort(collectionOrder);
  // A cycle's top is not drawn again under its child.
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
  const shared = sharedWithMe(rows).filter((c) => !c.parent || !byName.has(c.parent));
  for (const c of [...mine, ...shared]) visit(c);
  // What is left hangs off a cycle (cycleStart).
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

/** Memoised: the page beside it draws again on every keystroke in its search. */
export const LibraryTree = memo(function LibraryTree({
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
  /** `arriveFocus`: the picked row's focus key, so the person stays in the tree; `note`: the
   *  place's name, said on arrival. */
  onPick: (collection: string | null, arriveFocus: string, note: string) => void;
  /** A collection made here, in `org`: read the list again and open it. It can land after
   *  the tree is gone (a resize, or a move to another org). */
  onCreated: (collection: string, org: string) => void;
}) {
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
  // The way from the top to the place shown, which a move opens, once the list names it (a
  // person can close any of it after), and on which each list draws its step wherever it sits.
  const onPath = useMemo(() => {
    const shown = current ? byName.get(current) : undefined;
    return new Set(shown ? collectionPath(shown, byName).flatMap((p) => ("namespace" in p ? [p.namespace] : [])) : []);
  }, [byName, current]);
  const shownFor = useRef<string | null>(null);
  useEffect(() => {
    if (shownFor.current === current) return;
    if (current && !byName.has(current)) return;
    shownFor.current = current;
    if (current) setRevealed((prev) => withAll(prev, [...onPath]));
  }, [byName, current, onPath]);
  const isOpen = (key: string) => open.has(key) || revealed.has(key);
  const toggle = (key: string) => {
    if (isOpen(key)) {
      setOpen((prev) => without(prev, key));
      setRevealed((prev) => without(prev, key));
    } else {
      setOpen((prev) => withAll(prev, [key]));
    }
  };
  const tree = useMemo(() => placeTree(rows ?? []), [rows]);

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

  const branch = (c: CollectionEntry, depth: number): ReactNode => {
    const kids = tree.children.get(c.namespace) ?? [];
    const expanded = kids.length > 0 && isOpen(c.namespace);
    return (
      <li key={c.namespace} data-collection={c.namespace}>
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
        {expanded && <PlaceBranches items={kids} depth={depth + 1} onPath={onPath} branch={branch} />}
      </li>
    );
  };

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
      <PlaceBranches
        items={tree.mine}
        depth={0}
        onPath={onPath}
        branch={branch}
        label={{ id: "library-tree-mine", text: "Collections" }}
      />
      <PlaceBranches
        items={tree.shared}
        depth={0}
        onPath={onPath}
        branch={branch}
        label={{ id: "library-tree-shared", text: "Shared with you" }}
      />
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
});

/** A list of places in the tree (a section's, under its `label`, or a collection's): the
 *  first MORE_SHOWN, Show more adding MORE_SHOWN at a time, and the one on the way to the
 *  place shown wherever it sits. */
function PlaceBranches({
  items,
  depth,
  onPath,
  branch,
  label,
}: {
  items: CollectionEntry[];
  depth: number;
  /** The collections from the top to the place shown. */
  onPath: Set<string>;
  branch: (c: CollectionEntry, depth: number) => ReactNode;
  label?: { id: string; text: string };
}) {
  const extra = items.find((c) => onPath.has(c.namespace)) ?? null;
  const { shown, list, more, showMore } = useShowMore(items, MORE_SHOWN, extra);
  if (items.length === 0) return null;
  return (
    <>
      {label && (
        <p id={label.id} className="section-label library-tree-label">
          {label.text}
        </p>
      )}
      <ul ref={list} aria-labelledby={label?.id}>
        {shown.map((c) => branch(c, depth))}
        {more && (
          <li className="library-tree-row" style={{ "--depth": depth } as CSSProperties}>
            <span className="library-tree-twist" aria-hidden="true" />
            <Button type="empty" htmlType="button" onClick={showMore}>
              {more}
            </Button>
          </li>
        )}
      </ul>
    </>
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
  onPick: (collection: string | null, arriveFocus: string, note: string) => void;
}) {
  const focusKey = placeFocusKey(collection);
  return (
    <div className="library-tree-row" data-selected={here || undefined} style={{ "--depth": depth } as CSSProperties}>
      {onToggle ? (
        <IconButton
          icon={expanded ? "chevron-down" : "chevron-right"}
          size="xs"
          label={`Collections in ${label}`}
          aria-expanded={!!expanded}
          className="library-tree-twist"
          onClick={onToggle}
        />
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
        data-focus-key={focusKey}
        onOpen={() => onPick(collection, focusKey, label)}
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
    // Create is disabled without a title, or with one too long, and so is the form's submit.
    if (busy) return;
    setBusy(true);
    let r: CollectionCreated;
    try {
      r = await createCollection(org, "", { parent, title: trimmed, deriveName: true });
    } catch (err) {
      toastError(`Couldn't create ${trimmed}`, err);
      setBusy(false);
      field.current?.focus();
      return;
    }
    announceCreated(r, trimmed);
    onClose();
    onCreated(r.namespace, org);
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
