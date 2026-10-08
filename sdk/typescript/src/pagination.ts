/** Cursor pagination over the wire's two page shapes (`rows` or `results`, with `next_cursor`). */

export interface CursorPage<T> {
  rows?: T[];
  results?: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

/**
 * Walk a cursor-paginated op (`knowledge.list`, `conversation.list`,
 * `audit.list`, `code.search`) until the server stops returning `next_cursor`.
 * `fetchPage` receives the cursor to send.
 */
export async function* paginate<T>(
  fetchPage: (cursor: string | undefined) => Promise<CursorPage<T>>,
  opts: { maxPages?: number } = {},
): AsyncGenerator<T, void, void> {
  let cursor: string | undefined;
  const maxPages = opts.maxPages ?? Infinity;
  for (let page = 0; page < maxPages; page++) {
    const out = await fetchPage(cursor);
    for (const row of out.rows ?? out.results ?? []) yield row;
    const next = out.next_cursor ?? undefined;
    if (!next || next === cursor) return;
    cursor = next;
  }
}

/** Collect an async generator into an array (bounded by `limit`). */
export async function collect<T>(gen: AsyncGenerator<T, void, void>, limit = Infinity): Promise<T[]> {
  const out: T[] = [];
  if (limit <= 0) return out;
  for await (const x of gen) {
    out.push(x);
    if (out.length >= limit) break;
  }
  return out;
}
