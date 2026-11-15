/** The Library's read of several places at once (All documents, a search everywhere): one
 *  `knowledge.list` naming them all, and which of them it could not read. */
import { listKnowledge, type KnowledgeEntry, type KnowledgeListResponse } from "../api";

/** `namespace` and the 100 `namespaces` beside it: the most places one read names. */
export const MAX_PLACES_READ = 101;

/** Places read alone at once while a failed read finds which of them failed. */
const PROBES_AT_ONCE = 6;

/** A place the read left out, and why. */
export interface PlaceFailure {
  namespace: string;
  error: unknown;
}

export interface AcrossPage {
  rows: KnowledgeEntry[];
  next_cursor: string | null;
  /** The places the rows come from: those asked, without the ones that failed, or only the
   *  first when the server reads one place per call. The cursor holds for exactly these. */
  read: string[];
  failed: PlaceFailure[];
  /** The server ignored `namespaces` (a build a release behind): only the first was read. */
  oneAtATime: boolean;
}

type ListOpts = Omit<NonNullable<Parameters<typeof listKnowledge>[1]>, "namespaces" | "signal">;

/** Each row with its place: a server that predates several places sends none, and then
 *  every row is the first place's. */
function placed(page: KnowledgeListResponse, first: string): KnowledgeEntry[] {
  return (page.rows || []).map((r) => (r.namespace ? r : { ...r, namespace: first }));
}

/** One page of `places`' documents, newest first. A failed read finds the places that fail
 *  alone and reads the rest; it throws when all or none of them fail alone. */
export async function listAcross(
  org: string,
  places: string[],
  opts: ListOpts,
  signal: AbortSignal,
): Promise<AcrossPage> {
  const read = (names: string[]) =>
    listKnowledge(
      { org, namespace: names[0] },
      { ...opts, ...(names.length > 1 ? { namespaces: names.slice(1) } : {}), signal },
    );
  const done = (page: KnowledgeListResponse, names: string[], failed: PlaceFailure[]): AcrossPage => {
    const oneAtATime = names.length > 1 && (page.ignored_args ?? []).includes("namespaces");
    return {
      rows: placed(page, names[0]),
      next_cursor: page.next_cursor ?? null,
      read: oneAtATime ? names.slice(0, 1) : names,
      failed,
      oneAtATime,
    };
  };
  try {
    return done(await read(places), places, []);
  } catch (e) {
    if (signal.aborted || places.length < 2 || opts.cursor) throw e;
    const failed = await failingPlaces(org, places, signal);
    if (failed.length === 0 || failed.length === places.length) throw e;
    const gone = new Set(failed.map((f) => f.namespace));
    const rest = places.filter((p) => !gone.has(p));
    return done(await read(rest), rest, failed);
  }
}

/** The places among `places` that a read of one document refuses or fails. */
async function failingPlaces(org: string, places: string[], signal: AbortSignal): Promise<PlaceFailure[]> {
  const failed: PlaceFailure[] = [];
  let next = 0;
  const worker = async () => {
    while (next < places.length && !signal.aborted) {
      const namespace = places[next++];
      try {
        await listKnowledge({ org, namespace }, { limit: 1, fields: ["source_uri"], signal });
      } catch (error) {
        failed.push({ namespace, error });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PROBES_AT_ONCE, places.length) }, worker));
  if (signal.aborted) throw new DOMException("aborted", "AbortError");
  // In the order the places were asked, whichever answered first.
  return places.flatMap((p) => failed.filter((f) => f.namespace === p));
}
