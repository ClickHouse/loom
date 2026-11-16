import { useMemo, useRef, useState } from "react";
import { invalidateSharedCollections } from "./collectionPlaces";
import { isRefusal, type PlaceFailure } from "./libraryAcross";

/** Places a read was refused under the collections answer on screen (`kept`): later reads
 *  skip them and name them again, until the collections answer again (a grant may have caught
 *  up), which reads the list again through `reload`. */
export function useRefusedPlaces(kept: unknown, org: string, reload: () => void) {
  const [refused, setRefused] = useState<{ under: unknown; failures: PlaceFailure[] }>({ under: null, failures: [] });
  const skip = useMemo(() => (refused.under === kept ? refused.failures : []), [refused, kept]);
  // The answer on screen, for a read that lands after it was started.
  const keptNow = useRef(kept);
  keptNow.current = kept;
  // Whether the last read that landed names a refused place.
  const refusedShown = useRef(false);
  // The places ("org/namespace") whose refusal read the collections again, once each until a
  // read finds none refused (a place revoked again then reads them again).
  const askedAgainFor = useRef(new Set<string>());
  // A new answer while a refused place is named reads the list again. Done while rendering,
  // so when the answer also changes the places read, the page reads them once.
  const [seen, setSeen] = useState(kept);
  if (seen !== kept) {
    setSeen(kept);
    if (refusedShown.current) reload();
  }

  /** What a read started under the answer `under` found, or null when it failed. */
  const record = (under: unknown, page: { refused: PlaceFailure[]; failed: PlaceFailure[] } | null) => {
    refusedShown.current = !!page?.failed.some((f) => isRefusal(f.error));
    if (!page) return;
    if (!refusedShown.current) askedAgainFor.current.clear();
    const found = page.refused;
    // Kept only while that answer is still the one on screen: a later one may name a grant
    // that caught up.
    if (found.length === 0 || keptNow.current !== under) return;
    setRefused((prev) => ({ under, failures: [...(prev.under === under ? prev.failures : []), ...found] }));
    const first = found.filter((f) => !askedAgainFor.current.has(`${org}/${f.namespace}`));
    for (const f of first) askedAgainFor.current.add(`${org}/${f.namespace}`);
    if (first.length) invalidateSharedCollections();
  };
  return { skip, record };
}
