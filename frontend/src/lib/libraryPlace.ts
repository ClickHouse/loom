/** The Library's places as its URL names them: a collection (`?collection=`), All documents
 *  (`?place=all`), or neither for the header's namespace. */

/** All documents, where a collection's or the header namespace's name would go: never one,
 *  since a namespace name holds no `:`. */
export const ALL_DOCUMENTS = ":all";

/** The `place` value that names All documents in the address. */
const ALL_IN_URL = "all";

/** The query that opens `place`: a collection, All documents, or null for the namespace. */
export function placeQuery(place: string | null): Record<string, string> {
  if (place === ALL_DOCUMENTS) return { place: ALL_IN_URL };
  return place ? { collection: place } : {};
}

/** Whether the address names All documents. */
export function linkedAll(): boolean {
  return new URLSearchParams(window.location.search).get("place") === ALL_IN_URL;
}
