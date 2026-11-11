/**
 * How a collection is named outside the Library: by its title and its place, never as a
 * namespace. Its `col-…` name is its address, shown in details, and beside a title that looks
 * like another.
 *
 * The boot loads this module on demand, so the index chunk does not carry it.
 */
import { useEffect, useState } from "react";
import { listCollections, type CollectionEntry, type CollectionListPage, type MyNamespaceEntry } from "../api";
import { collectionTitle, looksRepeated, repeatedLooks } from "./collections";
import { isCollectionName, isPersonalCollection, type LandingTarget } from "./namespace";
import { offersCollection } from "./tokenScope";
import { invalidateShared, shared } from "./sharedReads";

const COLLECTIONS_KEY = "collections:";

/** The first page of the collections the caller can read (`collection.list`): one shared
 *  read per org per 30 s, however many views ask. */
export function sharedCollections(org: string): Promise<CollectionListPage> {
  return shared(`${COLLECTIONS_KEY}${org}`, 30_000, () => listCollections(org));
}

/** For a write that changes a collection: the next asker reads `collection.list` afresh. */
export function invalidateSharedCollections(): void {
  invalidateShared(COLLECTIONS_KEY);
}

/** Whether `m` is a direct grant on a shared collection, one the collection gate admits: not
 *  a claim-only or inherited row, not a pending one, not the person's own. */
function grantsSharedCollection(m: MyNamespaceEntry): boolean {
  return offersCollection(m) && !isPersonalCollection(m.namespace) && (m.status ?? "active") === "active";
}

/** Where an account that would land on Setup lands: the Library when it reads a shared
 *  collection. `collection.list` decides, since it applies the gate's rules (and holds those
 *  reached through a parent, a group or everyone); when it cannot be read, a direct grant in
 *  the account's own rows does. */
export async function landingWithCollections(org: string, mine: readonly MyNamespaceEntry[]): Promise<LandingTarget> {
  const page = await sharedCollections(org).catch(() => null);
  const shared = page ? page.collections.some((c) => !c.personal) : mine.some(grantsSharedCollection);
  return { view: shared ? "knowledge" : "setup", namespace: null };
}

/** How deep a place is walked before it stops: deeper than any tree the server allows. */
const MAX_DEPTH = 32;

/** The viewer's first page of `collection.list`, and whether it is the whole list. */
export interface CollectionsRead {
  rows: CollectionEntry[];
  complete: boolean;
}

/** Whether `namespace` is another person's own collection: a personal one the viewer's whole
 *  `collection.list` does not hold (an end user's own is there, as "My files"). Unknown, so
 *  false, until that list answers, and while more pages follow: the name is shown then. */
export function isOthersPersonal(namespace: string, read: CollectionsRead | null): boolean {
  if (!read?.complete || !isPersonalCollection(namespace)) return false;
  return !read.rows.some((c) => c.namespace === namespace);
}

/** A collection's title for its name: its `collection.list` row's ("My files" for the
 *  user's own, the one personal collection that list holds), else the title its list_mine
 *  row carries, else its name. */
export function collectionTitleOf(
  namespace: string,
  rows: readonly CollectionEntry[] | null,
  mine: readonly MyNamespaceEntry[] | null,
): string {
  const row = rows?.find((c) => c.namespace === namespace);
  if (row) return collectionTitle(row);
  return mine?.find((m) => m.namespace === namespace)?.title?.trim() || namespace;
}

/** Where a collection sits, its own title left out: the collections above it from the top
 *  of the Library, "Library › Papers" ("Library" for a top-level one). A parent the caller
 *  cannot read shows as "…", and the walk stops there. "" without its row (the list not read
 *  yet, failed, or not holding it): the place is not known, so none is claimed. */
export function collectionPlace(namespace: string, rows: readonly CollectionEntry[] | null): string {
  const byName = new Map((rows ?? []).map((c) => [c.namespace, c]));
  const row = byName.get(namespace);
  if (!row) return "";
  const titles: string[] = [];
  let parent = row.parent;
  for (let depth = 0; parent && depth < MAX_DEPTH; depth++) {
    const at = byName.get(parent);
    if (!at) {
      titles.unshift("…");
      break;
    }
    titles.unshift(collectionTitle(at));
    parent = at.parent;
  }
  return ["Library", ...titles].join(" › ");
}

/** The first page of the collections the caller reads (one shared read per page), or null
 *  until it answers or when it fails: callers then fall back to the list_mine title. */
export function useSharedCollections(org: string | null | undefined): CollectionEntry[] | null {
  return useSharedCollectionsRead(org)?.rows ?? null;
}

/** `useSharedCollections`, with whether the first page is the whole list. */
export function useSharedCollectionsRead(org: string | null | undefined): CollectionsRead | null {
  // Kept with the org it answers for: a name is unique within one org only, so another
  // org's rows never name this one's, not even for the render that switches.
  const [read, setRead] = useState<{ org: string; read: CollectionsRead } | null>(null);
  useEffect(() => {
    if (!org) return;
    let live = true;
    sharedCollections(org)
      .then((page) => live && setRead({ org, read: { rows: page.collections, complete: !page.next_cursor } }))
      .catch(() => live && setRead(null));
    return () => {
      live = false;
    };
  }, [org]);
  return read && read.org === org ? read.read : null;
}

/** `label`, with a collection's `col-…` name beside it when another of `names` (a collection,
 *  a namespace or a repository) looks the same, case, spacing and invisible characters aside. */
export function withTwinNames(label: (ns: string) => string, names: readonly string[]): (ns: string) => string {
  const repeated = repeatedLooks([...new Set(names)].map(label));
  return (ns) => {
    const shown = label(ns);
    // Already named: the label is the name, or ends with it ("the collection col-x").
    const named = shown === ns || shown.endsWith(` ${ns}`);
    return isCollectionName(ns) && !named && looksRepeated(shown, repeated) ? `${shown} (${ns})` : shown;
  };
}

/** Every collection the viewer knows of (its `collection.list` rows and list_mine rows), the
 *  names a look-alike is told apart from (`withTwinNames`), as the Library does. */
export function knownCollections(
  rows: readonly CollectionEntry[] | null,
  mine: readonly MyNamespaceEntry[] | null,
): string[] {
  return [...(rows ?? []).map((c) => c.namespace), ...(mine ?? []).map((m) => m.namespace).filter(isCollectionName)];
}

/** Whether the viewer is known to read a collection: a direct grant on one in its list_mine
 *  rows (`grantsSharedCollection`), or a shared one in `collection.list`, which also holds
 *  those reached through a parent, a group or everyone. False until one of them says so: a
 *  line about collections then appears once, never shown and taken back from someone who
 *  reads none. */
export function readsCollections(
  rows: readonly CollectionEntry[] | null,
  mine: readonly MyNamespaceEntry[] | null,
): boolean {
  // One's own collection is not one a `*` token could be told to read: never counted.
  return !!mine?.some(grantsSharedCollection) || !!rows?.some((c) => !c.personal);
}
