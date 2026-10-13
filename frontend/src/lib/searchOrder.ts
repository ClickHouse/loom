// The order a search shows on the Memories list, which Recall Lab explains row by row:
// the memories whose text holds the query (memory.list `contains`, newest first), then
// the ranked hits they do not already hold.

/** Keyword matches a search shows ahead of the ranked hits (a viewer's list page is 100). */
export const KEYWORD_PAGE = 100;

export function keywordThenRanked(keywordKeys: string[], rankedKeys: string[]): string[] {
  const keyword = new Set(keywordKeys);
  return [...keywordKeys, ...rankedKeys.filter((k) => !keyword.has(k))];
}
