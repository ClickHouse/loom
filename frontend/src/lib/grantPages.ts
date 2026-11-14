import { listGrants, type GrantEntry } from "../api";

/** Grants read per page, and the pages read on one namespace before a list counts as cut. */
export const GRANTS_PAGE = 500;
export const GRANTS_PAGES = 20;

/** The grants on one namespace. `cut`: more pages followed than were read. */
export interface GrantsAt {
  namespace: string;
  rows: GrantEntry[];
  cut?: boolean;
}

/** Every grant on `namespace` (`grants.list`, newest first), page by page, up to
 *  `GRANTS_PAGES` pages: the oldest grants, such as a creator's, are on the last page. */
export async function allGrants(id: { org: string; namespace: string }, signal?: AbortSignal): Promise<GrantsAt> {
  const rows: GrantEntry[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < GRANTS_PAGES; page++) {
    const r = await listGrants(id, { limit: GRANTS_PAGE, cursor, signal });
    rows.push(...(r.rows ?? []));
    cursor = r.next_cursor ?? null;
    if (!cursor) return { namespace: id.namespace, rows };
  }
  return { namespace: id.namespace, rows, cut: true };
}
