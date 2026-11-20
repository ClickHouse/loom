import { useEffect, useRef, useState } from "react";
import {
  bulkGetMemories,
  listMemories,
  searchMemory,
  type Identity,
  type MemoryDetail,
  type MemoryListEntry,
  type SearchHit,
} from "../api";
import { CARD_VALUE_CHARS } from "../components/MemoryCard";
import { errorRequestId } from "../lib/format";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { KEYWORD_PAGE, keywordThenRanked } from "../lib/memoriesSearch";

// The fields the page and its cards read. Field names are a quarter of a row, so a field
// the page does not read costs on every row. `sensitivity`, which marks a high row sent
// without its value, comes with every entry, so it is not named.
const MEMORIES_LIST_FIELDS = [
  "memory_key",
  "memory_type",
  "value",
  "tags",
  "created_at",
  "pinned",
  "agent",
  "user_id",
  "session_id",
  "tombstoned_at",
  "verified_by",
  "verification",
];

/** A tag and agent filter: what a search's keyword half reads with. */
export interface TagAgentFilter {
  tags: string[];
  agent: string;
}

/** What the list reads from the server: the tags, the agent and the status. */
export interface ListFilter extends TagAgentFilter {
  status: string;
}

type AnyFilter = TagAgentFilter & { status?: string };

/** The same tags (in order), agent and status. */
export function sameFilter(a: AnyFilter, b: AnyFilter): boolean {
  return (
    a.agent === b.agent &&
    a.status === b.status &&
    a.tags.length === b.tags.length &&
    a.tags.every((t, i) => t === b.tags[i])
  );
}

/** The filter object last seen with these values, so an effect keyed on it runs only when
 *  a value changes, not on every render that builds a new object. */
export function useSameFilter<T extends AnyFilter>(next: T): T {
  const [kept, setKept] = useState(next);
  if (sameFilter(kept, next)) return kept;
  setKept(next);
  return next;
}

// Shape a memory.get detail as a list entry, so a search hit fetched
// on-demand (one outside the preloaded window) renders through the same
// MemoryCard path as preloaded rows.
function detailToEntry(key: string, d: MemoryDetail): MemoryListEntry {
  return {
    memory_key: key,
    memory_type: d.memory_type || "semantic",
    confidence: d.confidence ?? 1,
    version: d.version ?? 0,
    created_at: d.created_at || "",
    updated_at: d.updated_at ?? null,
    expires_at: d.expires_at ?? null,
    tombstoned_at: null,
    pinned: d.pinned,
    tags: d.tags || [],
    agent: d.agent,
    user_id: d.user_id,
    session_id: d.session_id,
    value: d.value,
    source_trace: d.source_trace,
  };
}

/**
 * The Memories search: the rows holding the typed words, then the server's ranking, read
 * for `rawQuery` under the list's tag and agent filter. `entries` is the page the list
 * holds, so a hit outside it is read on its own.
 */
export function useMemorySearch(
  identity: Identity,
  rawQuery: string,
  filter: TagAgentFilter,
  entries: MemoryListEntry[] | null,
  memoriesVersion: number,
) {
  // Ordered array (not Set) to preserve the server's relevance ranking.
  // Null = no active search.
  const [searchHits, setSearchHits] = useState<string[] | null>(null);
  // Per-hit ranking evidence: the server's `explanation` and `score_components`, keyed by
  // memory_key, rendered under each card while a search is active so the list says why a
  // row is there.
  const [searchWhy, setSearchWhy] = useState<Map<string, SearchHit>>(new Map());
  // Full rows for search hits that fell outside the preloaded window,
  // fetched on-demand so they still render (keyed by memory_key).
  const [searchExtra, setSearchExtra] = useState<Map<string, MemoryListEntry>>(new Map());
  const [searchAbstain, setSearchAbstain] = useState<string | null>(null);
  // The rows whose value or key holds the typed text (newest first, ahead of the ranked
  // hits) and how many the namespace holds in all — a ranked search returns its top k, so
  // a word that 150 memories name would otherwise hide the newest of them.
  const [keywordTotal, setKeywordTotal] = useState(0);
  const [keywordKeys, setKeywordKeys] = useState<Set<string>>(new Set());
  // The text those rows hold: the query they were read for, not what the box says now.
  const [keywordFor, setKeywordFor] = useState("");
  const [searching, setSearching] = useState(false);
  // The keyword rows are on screen and the ranked search is still answering.
  const [ranking, setRanking] = useState(false);
  // The ranked search for the query being shown. A filter change runs the keyword read
  // again but awaits this same search: the filter is not one of its inputs, and each new
  // one costs an embedding.
  const rankedRef = useRef<{ key: string; promise: ReturnType<typeof searchMemory>; ac: AbortController } | null>(null);
  useEffect(() => () => rankedRef.current?.ac.abort(), []);
  // The ranked keys on screen, for the namespace and query they ranked: a search run again
  // for the same query (after a write here) keeps them while it ranks again.
  const rankedShownRef = useRef<{ key: string; keys: string[] } | null>(null);
  // Another namespace's results are not this one's: they go during render, before the new
  // namespace's first paint can use them.
  const namespaceKey = `${identity.org}/${identity.namespace}`;
  const [stateFor, setStateFor] = useState(namespaceKey);
  if (stateFor !== namespaceKey) {
    setStateFor(namespaceKey);
    setSearchHits(null);
    setSearchWhy(new Map());
    setSearchExtra(new Map());
    setKeywordKeys(new Set());
    setKeywordFor("");
    setKeywordTotal(0);
    setRanking(false);
    setSearching(false);
    setSearchAbstain(null);
  }
  // Latest preloaded entries, read inside the search effect without making
  // it a dependency (that would re-run search on every 8s poll).
  const entriesRef = useRef<MemoryListEntry[]>([]);

  useEffect(() => {
    // Only the natural-language portion is the search; `tag:` and `agent:` filter the list.
    // One character is not a query — the box waits for two, and a search a newer
    // keystroke made stale is aborted rather than raced (two answers would compete for
    // the same state).
    if (!rawQuery || rawQuery.trim().length < 2) {
      setSearchHits(null);
      setSearchAbstain(null);
      setSearchExtra(new Map());
      setSearchWhy(new Map());
      setKeywordKeys(new Set());
      setKeywordFor("");
      setKeywordTotal(0);
      setRanking(false);
      // A search cut short (the box cleared while it read) is not still searching.
      setSearching(false);
      rankedRef.current?.ac.abort();
      rankedRef.current = null;
      rankedShownRef.current = null;
      return;
    }
    let cancelled = false;
    const ac = new AbortController();
    const t = setTimeout(async () => {
      setSearching(true);
      try {
        // `explain: true` keeps `explanation` / `score_components` on the
        // wire above the excerpt k, so the list can say why each hit ranked where it did.
        // `legs: ["memory"]`: this page renders memory cards and never a session digest,
        // so the session-summary legs stay out.
        // The keyword list and the ranked search start together. The keyword rows paint as
        // soon as they arrive (a few hundred ms) and the ranked hits join them when the
        // search answers (it embeds the query first); a failed keyword read leaves the
        // ranked hits standing.
        // A local write (memoriesVersion) searches and ranks again, so a hit deleted or pinned
        // here shows as it now is; a filter change alone reuses the ranking.
        const rankedKey = `${identity.org}/${identity.namespace}\n${memoriesVersion}\n${rawQuery}`;
        if (rankedRef.current?.key !== rankedKey) {
          rankedRef.current?.ac.abort();
          const rac = new AbortController();
          const promise = searchMemory(identity, rawQuery, {
            top_k: 50,
            explain: true,
            legs: ["memory"],
            signal: rac.signal,
          });
          // Awaited below; a rejection before then is not unhandled, and is not kept for the
          // next run to reuse.
          promise.catch(() => {
            if (rankedRef.current?.promise === promise) rankedRef.current = null;
          });
          rankedRef.current = { key: rankedKey, promise, ac: rac };
        }
        const ranked = rankedRef.current.promise;
        const kw = await listMemories(identity, KEYWORD_PAGE, {
          contains: rawQuery.trim(),
          tags: filter.tags,
          agent: filter.agent || undefined,
          fields: MEMORIES_LIST_FIELDS,
          previewChars: CARD_VALUE_CHARS,
          signal: ac.signal,
        }).catch(() => null);
        if (cancelled) return;
        const kwEntries = kw?.entries ?? [];
        const kwKeys = new Set(kwEntries.map((e) => e.memory_key));
        const have = new Set(entriesRef.current.map((e) => e.memory_key));
        const kwExtra = kwEntries.filter((e) => !have.has(e.memory_key)).map((e) => [e.memory_key, e] as const);
        setKeywordKeys(kwKeys);
        setKeywordFor(rawQuery.trim());
        setKeywordTotal(kw?.total ?? 0);
        // The ranked keys shown for this same query, kept until the new ranking answers, so
        // the list does not shrink to the keyword rows and grow back after a write. Only
        // those the page or the keyword read holds: a copy read before a write is not today's.
        const shownKey = `${identity.org}/${identity.namespace}\n${rawQuery}`;
        const kept = (rankedShownRef.current?.key === shownKey ? rankedShownRef.current.keys : []).filter(
          (k) => have.has(k) || kwKeys.has(k),
        );
        if (kwKeys.size > 0) {
          setSearchHits(keywordThenRanked([...kwKeys], kept));
          setSearchExtra(new Map(kwExtra));
          if (kept.length === 0) setSearchWhy(new Map());
          // Rows hold the words: no low-confidence note from an earlier query.
          setSearchAbstain(null);
          setSearching(false);
          setRanking(true);
        }
        // A failed ranked search keeps the keyword rows on screen; with none, the list
        // empties as before.
        const r = await (kwKeys.size > 0 ? ranked.catch(() => null) : ranked);
        if (cancelled) return;
        setRanking(false);
        if (r === null) {
          // The new ranking failed: the hits kept from the last one are not today's, so the
          // list goes back to the keyword rows and the next run keeps none of them.
          rankedShownRef.current = null;
          if (kept.length > 0) {
            setSearchHits([...kwKeys]);
            setSearchExtra(new Map(kwExtra));
            setSearchWhy(new Map());
          }
          return;
        }
        const results = r.results || [];
        const hits = keywordThenRanked(
          kwEntries.map((e) => e.memory_key),
          results.map((h) => h.memory_key),
        );
        rankedShownRef.current = { key: shownKey, keys: results.map((h) => h.memory_key) };
        setSearchHits(hits);
        setSearchWhy(new Map(results.map((h) => [h.memory_key, h] as const)));
        // The server hedges (abstain) when the top hit is below threshold;
        // surface it so weak matches aren't read as confident answers.
        setSearchAbstain(
          r.abstain && kwKeys.size === 0 ? r.abstain_reason || "No strong match — showing best-effort results." : null,
        );
        // Server search spans the whole namespace, but the page only preloads
        // the top `limit` rows. Hydrate hits outside that window so they still
        // render, instead of being dropped by the key-set intersection below.
        // agent="*" resolves the row regardless of which agent wrote it.
        const missing = hits.filter((k) => !have.has(k) && !kwKeys.has(k));
        if (missing.length === 0) {
          setSearchExtra(new Map(kwExtra));
          return;
        }
        // One memory.bulk_get, not one memory.get per hit — the list path below already
        // reads this way.
        const bulk = await bulkGetMemories({ ...identity, agent: "*" }, missing, {
          signal: ac.signal,
        }).catch(() => ({
          results: {} as Record<string, MemoryDetail>,
          missing,
        }));
        const fetched = missing.map((k) => {
          const d = bulk.results[k];
          return d ? detailToEntry(k, d) : null;
        });
        if (cancelled) return;
        setSearchExtra(
          new Map([
            ...kwExtra,
            ...fetched.filter((e): e is MemoryListEntry => e !== null).map((e) => [e.memory_key, e] as const),
          ]),
        );
      } catch {
        if (!cancelled) {
          rankedShownRef.current = null;
          setSearchHits([]);
          setSearchAbstain(null);
          setSearchExtra(new Map());
          setSearchWhy(new Map());
          setKeywordKeys(new Set());
          setKeywordFor("");
          setKeywordTotal(0);
          setRanking(false);
        }
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
      ac.abort();
    };
  }, [rawQuery, identity, filter, memoriesVersion]);

  // Keep entriesRef current for the search effect (which reads it without
  // subscribing, to avoid re-running on every poll refresh).
  useEffect(() => {
    entriesRef.current = entries ?? [];
  }, [entries]);

  return {
    searchHits,
    searchWhy,
    searchExtra,
    searchAbstain,
    keywordTotal,
    keywordKeys,
    keywordFor,
    searching,
    ranking,
  };
}

/**
 * The Memories list's server read: a page of the namespace, or, under a filter, its newest
 * 1,000 matches in one read, refreshed every 8 s. `retry` reads the first page again.
 */
export function useMemoryListRead({
  identity,
  filter,
  filtered,
  page,
  pageSize,
  showDeleted,
  memoriesVersion,
}: {
  identity: Identity;
  filter: ListFilter;
  filtered: boolean;
  page: number;
  pageSize: number;
  showDeleted: boolean;
  memoriesVersion: number;
}) {
  const [entries, setEntries] = useState<MemoryListEntry[] | null>(null);
  // First-page read failure, shown inline with a Retry.
  const [loadError, setLoadError] = useState<{ text: string; requestId: string } | null>(null);
  // A filtered read the server cut short: the list says it shows the newest matches.
  const [truncated, setTruncated] = useState(false);
  // How many rows match a tag or agent filter on the server, when that count says more than
  // the rows read: the read was cut short (a viewer token reads fewer rows than asked) and
  // the server matched the tags as the page does. Otherwise the rows on screen are the count.
  const [serverMatches, setServerMatches] = useState<number | null>(null);
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set());
  const knownKeysRef = useRef<Set<string>>(new Set());
  // Values the list response did not carry (a server predating the preview
  // field), read back in ONE memory.bulk_get for the page instead of one
  // memory.get per card.
  const [bulkValues, setBulkValues] = useState<Map<string, unknown>>(new Map());
  const fetchOnceRef = useRef<(isFirst: boolean) => Promise<void>>(async () => {});
  // The list blanks only for a new namespace or view; a new page or filter keeps the
  // rows on screen (marked busy) until the next ones land, and the search keeps focus.
  const shownScope = useRef("");
  const [refreshing, setRefreshing] = useState(false);
  // The page and page size the browsed rows on screen were read as: the pager pages from
  // that page while it is still the size shown, so a second Next while a page reads asks
  // for the same page and the range says the rows under it.
  const [entriesAt, setEntriesAt] = useState({ page: 0, size: 0 });
  // The namespace's total from browsed reads (the first and every refresh): a filter's count
  // is not it.
  const [browsedTotal, setBrowsedTotal] = useState<number | undefined>(undefined);
  // The filter the rows on screen were read for: until a read of the current one lands,
  // the old rows stay up and "nothing matches" is not said.
  const [readFor, setReadFor] = useState<ListFilter | null>(null);
  // The namespace (`org/namespace`) the rows on screen were read in: until a read there
  // lands, the rows are the last namespace's.
  const [listedIn, setListedIn] = useState("");
  // A filtered list's rows are its own once its read has answered.
  const filterRead = readFor !== null && sameFilter(readFor, filter);
  // A filtered list holds every match already: its pages and page size need no read.
  const readPage = filtered ? 0 : page;
  const readSize = filtered ? 0 : pageSize;

  useAbortableEffect(
    (signal, live) => {
      const scope = `${identity.org}/${identity.namespace}/${identity.agent}/${showDeleted}`;
      if (shownScope.current !== scope) {
        shownScope.current = scope;
        setEntries(null);
        setBrowsedTotal(undefined);
      } else {
        setRefreshing(true);
      }
      setBulkValues(new Map());
      knownKeysRef.current = new Set();
      setNewKeys(new Set());

      const fillMissingValues = async (rows: MemoryListEntry[]) => {
        const missing = rows.filter((e) => e.value === undefined).map((e) => e.memory_key);
        if (missing.length === 0) return;
        try {
          const b = await bulkGetMemories({ ...identity, agent: "*" }, missing, { signal });
          if (!live()) return;
          setBulkValues((prev) => {
            const next = new Map(prev);
            for (const [k, d] of Object.entries(b.results)) next.set(k, d.value);
            return next;
          });
        } catch {
          /* cards fall back to their own memory.get */
        }
      };

      // The namespace version the last full answer carried. The 8 s refresh sends it and
      // repaints only when it moved; an unchanged namespace answers in ~200 bytes with no
      // page, total or today.
      let knownVersion = "";
      const fetchOnce = async (isFirst: boolean) => {
        try {
          // Recently-deleted is a bounded 14-day grace set; fetch it in one shot
          // (no live-total pagination, which would be wrong for the deleted view).
          // A filtered list is one read of its newest 1,000 matches, with no pages.
          const lim = showDeleted ? 500 : filtered ? 1000 : readSize;
          const off = showDeleted || filtered ? 0 : readPage * readSize;
          const r = await listMemories(identity, lim, {
            include_tombstoned: showDeleted,
            offset: off,
            tags: filter.tags,
            agent: filter.agent || undefined,
            signal,
            known_version: isFirst ? undefined : knownVersion || undefined,
            fields: MEMORIES_LIST_FIELDS,
            previewChars: CARD_VALUE_CHARS,
          });
          if (!live()) return;
          if (r.unchanged) return;
          knownVersion = r.namespace_version ?? "";
          setTruncated(filtered && r.truncated);
          if (filtered) {
            const ignored = r.ignored_args ?? [];
            setServerMatches(r.truncated && !ignored.includes("tags_every") ? r.total : null);
          }
          // The namespace's total follows every browsed answer, the 8 s refresh included.
          if (!filtered && !showDeleted) setBrowsedTotal(r.total);
          // Fill before painting so a list without values never sends 100 cards to
          // their per-card fallback while the one bulk read is in flight.
          await fillMissingValues(r.entries);
          if (!live()) return;
          if (isFirst) {
            knownKeysRef.current = new Set(r.entries.map((e) => e.memory_key));
            setEntries(r.entries);
            setListedIn(`${identity.org}/${identity.namespace}`);
            setEntriesAt({ page: off / lim, size: lim });
            setReadFor(filter);
            setLoadError(null);
            // Next comes back with the rows it read, not before them.
            setRefreshing(false);
          } else {
            // Any key we haven't seen before is "new".
            const fresh = new Set<string>();
            for (const e of r.entries) {
              if (!knownKeysRef.current.has(e.memory_key)) {
                fresh.add(e.memory_key);
                knownKeysRef.current.add(e.memory_key);
              }
            }
            if (fresh.size > 0) {
              setEntries(r.entries);
              setNewKeys((prev) => new Set([...prev, ...fresh]));
              // Clear the "new" highlight after 4s, unless the effect is left first. A timer
              // that fires drops its abort listener, so a long-lived poll does not pile them up.
              const cancel = () => clearTimeout(timer);
              const timer = setTimeout(() => {
                signal.removeEventListener("abort", cancel);
                setNewKeys((prev) => {
                  const next = new Set(prev);
                  fresh.forEach((k) => next.delete(k));
                  return next;
                });
              }, 4000);
              signal.addEventListener("abort", cancel, { once: true });
            }
          }
        } catch (e) {
          if (!live()) return;
          if (isFirst) {
            setRefreshing(false);
            // Say so inline — an empty list that is really a failed read looked like "no
            // memories yet".
            setLoadError({ text: humanizeError(e, { read: true }), requestId: errorRequestId(e) });
            setEntries([]);
          }
        }
      };

      fetchOnceRef.current = fetchOnce;
      return fetchOnce(true);
    },
    [identity, readPage, readSize, memoriesVersion, showDeleted, filter],
  );
  // The 8 s refresh sleeps while the tab is hidden and catches up once on
  // return; it always runs the current effect's fetchOnce, so a response
  // for a superseded namespace/page is dropped by that effect's live().
  useVisiblePoll(() => {
    void fetchOnceRef.current(false);
  }, 8000);

  const retry = () => {
    setLoadError(null);
    setEntries(null);
    void fetchOnceRef.current(true);
  };

  return {
    entries,
    loadError,
    truncated,
    serverMatches,
    newKeys,
    bulkValues,
    refreshing,
    entriesAt,
    browsedTotal,
    filterRead,
    listedIn,
    retry,
  };
}
