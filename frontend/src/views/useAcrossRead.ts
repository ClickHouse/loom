import { useMemo, useState } from "react";
import { isRefusal, MAX_PLACES_READ, type PlaceFailure } from "../lib/libraryAcross";
import { useRefusedPlaces } from "../lib/useRefusedPlaces";
import { type ReadableCollections, treeOrder } from "./LibraryCollections";

/** What a read of several places read: the places the rows and the cursor come from, those
 *  that failed, and whether the server read only the first. `key` is what it was read for. */
export interface AcrossReport {
  key: string;
  read: string[];
  failed: PlaceFailure[];
  oneAtATime: boolean;
}

/** The Library's places to read and what the read on screen made of them. `across`: the
 *  list reads every place (All documents, or a search everywhere), else `namespace` alone.
 *  `rowsScope`: the `readKey` the rows on screen were read for. `reload` is called while
 *  rendering when a new collections answer lands while a refused place is named; it must
 *  only set this component's state, so that render starts one read. */
export function useAcrossRead({
  org,
  namespace,
  shelf,
  collections,
  across,
  rowsScope,
  reload,
}: {
  org: string;
  namespace: string;
  /** The header's namespace when it holds documents, else null. */
  shelf: string | null;
  collections: ReadableCollections;
  across: boolean;
  rowsScope: string;
  reload: () => void;
}) {
  // Every place the person reads in the Library, in the tree's order: the header's namespace,
  // then My files and the collections. Waits for the collections' first answer.
  const everyPlace = useMemo(() => {
    const out = new Set<string>(shelf ? [shelf] : []);
    for (const { c } of treeOrder(collections.kept?.rows ?? [])) out.add(c.namespace);
    return [...out];
  }, [shelf, collections.kept]);
  const [readOf, setReadOf] = useState<AcrossReport | null>(null);
  const refusals = useRefusedPlaces(collections.kept, org, across && !!readOf?.failed.some((f) => isRefusal(f.error)));
  // A new collections answer while a refused place is named reads the list again: a grant
  // that lagged behind the list may have caught up.
  if (refusals.reread) reload();
  // The places one read asks for: up to MAX_PLACES_READ it can read, a refused one not
  // counted; the rest are said to be left out.
  const everyRead = useMemo(() => {
    const skip = new Set(refusals.skip.map((f) => f.namespace));
    const out: string[] = [];
    let readable = 0;
    for (const p of everyPlace) {
      if (!skip.has(p)) {
        if (readable === MAX_PLACES_READ) break;
        readable += 1;
      }
      out.push(p);
    }
    return out;
  }, [everyPlace, refusals.skip]);
  const placesCut = everyPlace.length - everyRead.length;
  const readPlaces = across ? everyRead : [namespace];
  // What the rows on screen were read for: the org and the places, in order.
  const readKey = `${org}/${readPlaces.join("\n")}`;
  // The read on screen's own report; one for other places is not this list's.
  const report = readOf?.key === readKey && rowsScope === readKey ? readOf : null;
  const leftOut = report?.failed ?? [];
  // Whether every place the person reads is known: not while the collections failed or stop early.
  const placesKnown = !!collections.kept && !collections.kept.truncated && !collections.error;
  // Every place the person reads was read, so "none anywhere" is true of the Library.
  const wholeRead = !!report && leftOut.length === 0 && !report.oneAtATime && placesCut === 0 && placesKnown;
  // A filter over a read of several places that left some out matched only the places read.
  const partPlaces = across && !wholeRead;
  // A passage search everywhere reads the places the list read (not one refused), and waits
  // while the list reads them; here, the place shown.
  const passagesIn = across ? (report?.read ?? null) : [namespace];
  return {
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
  };
}
