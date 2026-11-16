import { useMemo, useRef, useState } from "react";
import { invalidateSharedCollections } from "./collectionPlaces";
import { isRefusal, type PlaceFailure } from "./libraryAcross";

/** Places a read was refused under the collections answer on screen (`kept`): later reads
 *  skip them and name them again, until the collections answer again (a grant may have caught
 *  up). `namedRefusal`: the list on screen names a refused place. `reread` is true for the one
 *  render in which a new answer lands while it does: the caller reads the list again in that
 *  render, not in an effect, which would start a second read when the places change too. */
export function useRefusedPlaces<T>(kept: T, org: string, namedRefusal: boolean) {
  const [refused, setRefused] = useState<{ under: T | null; failures: PlaceFailure[] }>({ under: null, failures: [] });
  const skip = useMemo(() => (refused.under === kept ? refused.failures : []), [refused, kept]);
  // The answer on screen, for a read that lands after it was started.
  const keptNow = useRef(kept);
  keptNow.current = kept;
  // The places ("org/namespace") whose refusal read the collections again, once each until a
  // read finds none refused (a place revoked again then reads them again).
  const askedAgainFor = useRef(new Set<string>());
  // Told while rendering, so when the new answer also changes the places read, the caller's
  // reload and that change start one read.
  const [seen, setSeen] = useState(kept);
  const reread = seen !== kept && namedRefusal;
  if (seen !== kept) setSeen(kept);

  /** What a read started under the answer `under` found. */
  const record = (under: T, page: { refused: PlaceFailure[]; failed: PlaceFailure[] }) => {
    if (!page.failed.some((f) => isRefusal(f.error))) askedAgainFor.current.clear();
    const found = page.refused;
    // Kept only while that answer is still the one on screen: a later one may name a grant
    // that caught up.
    if (found.length === 0 || keptNow.current !== under) return;
    setRefused((prev) => ({ under, failures: [...(prev.under === under ? prev.failures : []), ...found] }));
    const first = found.filter((f) => !askedAgainFor.current.has(`${org}/${f.namespace}`));
    for (const f of first) askedAgainFor.current.add(`${org}/${f.namespace}`);
    if (first.length) invalidateSharedCollections();
  };
  return { skip, reread, record };
}
