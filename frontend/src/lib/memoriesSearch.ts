// How the Memories list searches, which Recall Lab repeats to explain it row by row:
// the query text it sends, and the order it shows rows in — the memories whose text
// holds the query (memory.list `contains`, newest first), then the ranked hits they do
// not already hold.

import { listMemories, storedToken, type Identity, type SearchHit } from "../api";
import { tokenReadScope } from "../views/assistant/tokenScope";
import { SENTINEL_AGENTS } from "./format";

/** Keyword matches a search shows ahead of the ranked hits (a viewer's list page is 100). */
export const KEYWORD_PAGE = 100;

/** Whether a row names its writer in full: a server that predates the fields, a shared row
 *  (`_shared`, `*`) and a chunk without them do not. */
export function namesWriter(r: { agent?: string; user_id?: string; session_id?: string }): boolean {
  return Boolean(r.agent && !SENTINEL_AGENTS.has(r.agent) && r.agent !== "_shared" && r.user_id && r.session_id);
}

export type SearchWriter = Pick<SearchHit, "namespace" | "agent" | "user_id" | "session_id">;

/** Older search responses name the agent but may omit its user or session. */
export async function resolveSearchWriter(
  identity: Identity,
  hit: SearchHit,
  signal: AbortSignal,
): Promise<SearchWriter | undefined> {
  const { agent, user_id, session_id } = hit;
  if (!agent || SENTINEL_AGENTS.has(agent) || agent === "_shared" || hit.memory_key.startsWith("chunk:")) return;
  if (namesWriter(hit)) return hit;

  // A token that narrows reads is refused the `*` wildcard (`resolve_read_filters`): send the
  // pinned user, and give up when no user (or, for an own-session token, no session) is known.
  const scope = tokenReadScope(storedToken());
  const uid = user_id || scope?.readUserId || (scope?.readScope ? "" : "*");
  const sid = session_id || (scope?.readScope === "own_session" ? "" : "*");
  if (!uid || !sid) return;

  const namespace = hit.namespace || identity.namespace;
  const page = await listMemories(
    { ...identity, namespace, user_id: uid, session_id: sid },
    KEYWORD_PAGE,
    { agent, prefix: hit.memory_key, include_profile: true, fields: ["agent", "user_id", "session_id"], signal },
  ).catch(() => null);
  if (!page || signal.aborted || page.truncated || page.total > page.entries.length) return;
  const matches = page.entries.filter(
    (row) =>
      row.memory_key === hit.memory_key &&
      (!row.agent || row.agent === agent) &&
      (!user_id || !row.user_id || row.user_id === user_id) &&
      (!session_id || !row.session_id || row.session_id === session_id),
  );
  const [row] = matches;
  if (matches.length === 1 && row && namesWriter(row)) {
    return { agent: row.agent, user_id: row.user_id, session_id: row.session_id, namespace };
  }
}

/** A row's own id: `(namespace, agent, user, session, key)`, which is what two rows of one
 *  key differ by; `[key]` alone when the row does not name its writer. The two shapes differ
 *  in length, so a key cannot make one pass for the other. */
export function rowIdentity(
  namespace: string,
  r: { memory_key: string; agent?: string; user_id?: string; session_id?: string; namespace?: string },
): string {
  return JSON.stringify(
    namesWriter(r) ? [r.namespace || namespace, r.agent, r.user_id, r.session_id, r.memory_key] : [r.memory_key],
  );
}

/** Every id a row answers to: its own and its key's. A hit that names no writer is the key
 *  alone (the old match, which keeps every row of the key); a hit that names one matches
 *  only the row it names. */
export function rowIds(namespace: string, r: Parameters<typeof rowIdentity>[1]): string[] {
  return [rowIdentity(namespace, r), JSON.stringify([r.memory_key])];
}

export function keywordThenRanked(keywordKeys: string[], rankedKeys: string[]): string[] {
  return [...new Set([...keywordKeys, ...rankedKeys])];
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
