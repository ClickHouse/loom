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

/** A search box's text split into what the server is asked and the filters the list
 *  applies itself: `tag:work coffee` searches "coffee" among memories tagged `work`;
 *  `agent:foo bar` does the same for an agent. Whitespace runs count as one space. */
export function parseSearchQuery(query: string): { text: string; tags: string[]; agent: string | null } {
  const tags: string[] = [];
  let agent: string | null = null;
  const rest: string[] = [];
  for (const tok of query.trim().split(/\s+/).filter(Boolean)) {
    const mTag = /^tag:(.+)$/.exec(tok);
    const mAgent = /^agent:(.+)$/.exec(tok);
    if (mTag) tags.push(mTag[1].toLowerCase());
    else if (mAgent) agent = mAgent[1].toLowerCase();
    else rest.push(tok);
  }
  return { text: rest.join(" "), tags, agent };
}
