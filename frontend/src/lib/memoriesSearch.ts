// How the Memories list searches, which Recall Lab repeats to explain it row by row:
// the query text it sends, and the order it shows rows in — the memories whose text
// holds the query (memory.list `contains`, newest first), then the ranked hits they do
// not already hold.

/** Keyword matches a search shows ahead of the ranked hits (a viewer's list page is 100). */
export const KEYWORD_PAGE = 100;

export function keywordThenRanked(keywordKeys: string[], rankedKeys: string[]): string[] {
  const keyword = new Set(keywordKeys);
  return [...keywordKeys, ...rankedKeys.filter((k) => !keyword.has(k))];
}

/** A parsed search: the text the server is asked, and the tag and agent filters. */
export type SearchQuery = { text: string; tags: string[]; agent: string | null };

/** A search box's text split into what the server is asked and the filters the list
 *  applies itself: `tag:work coffee` searches "coffee" among memories tagged `work`;
 *  `agent:foo bar` does the same for an agent. Whitespace runs count as one space. The
 *  prefixes read in any case ("TAG:work"); the values keep the case they were typed in,
 *  and a caller matching them ignores case. */
export function parseSearchQuery(query: string): SearchQuery {
  const tags: string[] = [];
  let agent: string | null = null;
  const rest: string[] = [];
  for (const tok of query.trim().split(/\s+/).filter(Boolean)) {
    const mTag = /^tag:(.+)$/i.exec(tok);
    const mAgent = /^agent:(.+)$/i.exec(tok);
    if (mTag) tags.push(mTag[1]);
    else if (mAgent) agent = mAgent[1];
    else rest.push(tok);
  }
  return { text: rest.join(" "), tags, agent };
}

/** The query with `token` added at the end, or taken out wherever it is (any case). */
export function toggleQueryToken(query: string, token: string): string {
  const tokens = query.trim().split(/\s+/).filter(Boolean);
  const kept = tokens.filter((t) => t.toLowerCase() !== token.toLowerCase());
  return (kept.length < tokens.length ? kept : [...tokens, token]).join(" ");
}

// A search another surface opens Memories with (an assistant card's `agent:<name>`), taken
// by the list once it draws.
let pendingSearch: string | null = null;

/** Open Memories searching for `query`, in place of what its search held. */
export function openMemoriesSearching(query: string, navigate: () => void): void {
  pendingSearch = query;
  navigate();
}

/** The search Memories was opened with, once. */
export function takePendingSearch(): string | null {
  const q = pendingSearch;
  pendingSearch = null;
  return q;
}
