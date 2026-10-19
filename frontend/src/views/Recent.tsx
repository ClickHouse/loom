import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Pagination } from "@clickhouse/click-ui/Pagination";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import {
  getMemory,
  listMemories,
  bulkGetMemories,
  pinMemory,
  deleteMemory,
  restoreMemory,
  conflictKeys as fetchConflictKeys,
  searchMemory,
  isAuthDisabled,
  type MemoryListEntry,
  type MemoryDetail,
  type SearchHit,
} from "../api";
import { errorRequestId } from "../lib/format";
import { useAbortableEffect, useVisiblePoll } from "../lib/hooks";
import { useStore, useIdentity } from "../lib/state";
import { MemoryCard } from "../components/MemoryCard";
import { SearchWhy } from "../components/SearchWhy";
import { Alert, Chip, PageHeader, SectionHeading } from "../components/ui";
import { SkeletonCard } from "../components/Skeleton";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../components/Toast";
import { confirmAction } from "../components/ConfirmDialog";
import { dayLabel, displayAgent, humanizeError, searchSummary, SENTINEL_AGENTS } from "../lib/format";
import { useKeyboard } from "../lib/hooks";
import { AddMemoryDialog } from "../flows/AddMemoryDialog";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { ScopeBadge } from "../components/ScopeBadge";
import { NamespaceAuditPanel } from "../components/NamespaceAuditPanel";
import { MentorPanel } from "../components/MentorPanel";
import { Unavailable } from "../components/ui";
import { READ_ONLY_TIP, canWrite, useNamespaceRole } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { KEYWORD_PAGE, keywordThenRanked, parseSearchQuery } from "../lib/memoriesSearch";
import { shortcutOff } from "../lib/shortcuts";

// Server caps memory.list at 1000 rows/call (MemoryListInput.limit le=1000).
// The browse feed pages up to this; past it, search covers the whole namespace.
// Page size for the paginated browse feed (server caps memory.list at 1000/call).
const PAGE_SIZE = 100;
// The fields the page and its cards read, and the preview length a card shows (140
// characters; the base64 envelope check needs 64). Unprojected, a page of a hundred is
// 166 kB of JSON — 2 KB previews, entities, timestamps nothing reads. Field names are a
// quarter of a row, so a field the page does not read costs on every row. `sensitivity`,
// which marks a high row sent without its value, comes with every entry, so it is not named.
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
const MEMORIES_PREVIEW_CHARS = 160;

// The cards the first screen shows. A page of a hundred mounts them all in one task
// (395–681 ms of long tasks at 4x CPU, LCP 0.9–1.4 s); the first screen is about two
// dozen. Those mount with the page, the rest in a transition after first paint — same
// DOM, same scroll, same find-in-page once it settles.
const FIRST_SCREEN_CARDS = 12; // A screen shows eight or nine

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

const SHOWN_GROUP = "memories-shown";

export function Recent() {
  const identity = useIdentity();
  // Viewers see the page read-only — the write CTAs are disabled with the reason, not
  // offered and then refused by the server.
  const role = useNamespaceRole(identity.namespace);
  const readOnly = !canWrite(role);
  const inspectedKey = useStore((s) => s.inspectedMemoryKey);
  const inspect = useStore((s) => s.inspect);
  // Bumped on memory writes from any surface so we re-fetch immediately
  // instead of waiting for the polling cycle.
  const memoriesVersion = useStore((s) => s.memoriesVersion);
  // Re-fetch the current page after a local mutation (bump drives the fetch
  // effect below), instead of reloading a fixed slice that ignores pagination.
  const bumpMemoriesVersion = useStore((s) => s.bumpMemoriesVersion);
  const [entries, setEntries] = useState<MemoryListEntry[] | null>(null);
  // First paint mounts the first screen; the rest follow in a transition (see
  // FIRST_SCREEN_CARDS).
  const [firstScreenOnly, setFirstScreenOnly] = useState(true);
  useEffect(() => {
    if (!firstScreenOnly || entries === null) return;
    let raf = 0;
    raf = requestAnimationFrame(() => {
      startTransition(() => setFirstScreenOnly(false));
    });
    return () => cancelAnimationFrame(raf);
  }, [firstScreenOnly, entries]);
  // First-page read failure, shown inline with a Retry.
  const [loadError, setLoadError] = useState<{ text: string; requestId: string } | null>(null);
  // True namespace-scope total (ignores the page `limit`), from memory.list.
  const [total, setTotal] = useState(0);
  // Of `total`, how many were created today (server-computed, not the page).
  const [todayCount, setTodayCount] = useState(0);
  const [newKeys, setNewKeys] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [pinnedOnly, setPinnedOnly] = useState(false);
  // Surfaces tombstoned rows + a Restore action in the 14-day grace window.
  const [showDeleted, setShowDeleted] = useState(false);
  // The list reloads behind the loading skeleton, which unmounts the toolbar; the chosen
  // option takes focus back once the rows are in.
  const refocusShown = useRef(false);
  useEffect(() => {
    if (entries === null || !refocusShown.current) return;
    refocusShown.current = false;
    document.querySelector<HTMLElement>(`#${SHOWN_GROUP} [aria-pressed="true"]`)?.focus();
  }, [entries]);
  // Tag filter from the global store — set by clicking a tag chip in the
  // Inspector, shown as a dismissable chip at the top of the list.
  const memoryTagFilter = useStore((s) => s.memoryTagFilter);
  const setMemoryTagFilter = useStore((s) => s.setMemoryTagFilter);
  // Per-agent filter chip — null means "all agents." Derived from the
  // entries actually present, not a static list.
  const [agentFilter, setAgentFilter] = useState<string | null>(null);
  // Zero-indexed page into the recency-ordered namespace list.
  const [page, setPage] = useState(0);
  const [conflictKeys, setConflictKeys] = useState<Set<string>>(new Set());
  const [addOpen, setAddOpen] = useState(false);
  const [query, setQuery] = useState("");
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
  const [keywordKeys, setKeywordKeys] = useState<Set<string>>(new Set());
  const [keywordTotal, setKeywordTotal] = useState(0);
  const [searching, setSearching] = useState(false);
  // The keyword rows are on screen and the ranked search is still answering.
  const [ranking, setRanking] = useState(false);
  const knownKeysRef = useRef<Set<string>>(new Set());
  // Latest preloaded entries, read inside the search effect without making
  // it a dependency (that would re-run search on every 8s poll).
  const entriesRef = useRef<MemoryListEntry[]>([]);
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // `/` focuses the inline search anywhere on Memories.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || shortcutOff(e)) return;
      const t = e.target;
      if (t instanceof HTMLElement && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable))
        return;
      e.preventDefault();
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Inline search: debounced semantic match on the server; the result
  // becomes a key-set filter on the already-loaded entries, so day
  // grouping, pinned star, conflict badge, and selection state stay intact.
  //
  // Query language:
  //   `tag:work`              — only memories tagged `work`
  //   `tag:work tag:urgent`   — AND-intersection (both tags)
  //   `tag:work coffee`       — semantic search for "coffee" within tag work
  //   `agent:foo bar`         — same shape for agent filter
  //
  // tag/agent filters apply client-side over the semantic-search result
  // set OR the raw entries list, and intersect with the chip selections.
  const { rawQuery, queryTags, queryAgent } = useMemo(() => {
    const parsed = parseSearchQuery(query);
    return { rawQuery: parsed.text, queryTags: parsed.tags, queryAgent: parsed.agent };
  }, [query]);

  useEffect(() => {
    // Only the natural-language portion goes to the server; structured
    // filters are applied locally.
    // One character is not a query — the box waits for two, and a search a newer
    // keystroke made stale is aborted rather than raced (two answers would compete for
    // the same state).
    if (!rawQuery || rawQuery.trim().length < 2) {
      setSearchHits(null);
      setSearchAbstain(null);
      setSearchExtra(new Map());
      setSearchWhy(new Map());
      setKeywordKeys(new Set());
      setKeywordTotal(0);
      setRanking(false);
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
        const ranked = searchMemory(identity, rawQuery, {
          top_k: 50,
          explain: true,
          legs: ["memory"],
          signal: ac.signal,
        });
        ranked.catch(() => {}); // awaited below; a rejection before then is not unhandled
        const kw = await listMemories(identity, KEYWORD_PAGE, {
          contains: rawQuery.trim(),
          fields: MEMORIES_LIST_FIELDS,
          previewChars: MEMORIES_PREVIEW_CHARS,
          signal: ac.signal,
        }).catch(() => null);
        if (cancelled) return;
        const kwEntries = kw?.entries ?? [];
        const kwKeys = new Set(kwEntries.map((e) => e.memory_key));
        const have = new Set(entriesRef.current.map((e) => e.memory_key));
        const kwExtra = kwEntries.filter((e) => !have.has(e.memory_key)).map((e) => [e.memory_key, e] as const);
        setKeywordKeys(kwKeys);
        setKeywordTotal(kw?.total ?? 0);
        if (kwKeys.size > 0) {
          setSearchHits(kwEntries.map((e) => e.memory_key));
          setSearchExtra(new Map(kwExtra));
          setSearchWhy(new Map());
          setSearching(false);
          setRanking(true);
        }
        // A failed ranked search keeps the keyword rows on screen; with none, the list
        // empties as before.
        const r = await (kwKeys.size > 0 ? ranked.catch(() => null) : ranked);
        if (cancelled) return;
        setRanking(false);
        if (r === null) return;
        const results = r.results || [];
        const hits = keywordThenRanked(
          kwEntries.map((e) => e.memory_key),
          results.map((h) => h.memory_key),
        );
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
        const bulk = await bulkGetMemories({ ...identity, agent: "*" }, missing, { signal: ac.signal }).catch(() => ({
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
          setSearchHits([]);
          setSearchAbstain(null);
          setSearchExtra(new Map());
          setSearchWhy(new Map());
          setKeywordKeys(new Set());
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
  }, [rawQuery, identity]);

  // Keep entriesRef current for the search effect (which reads it without
  // subscribing, to avoid re-running on every poll refresh).
  useEffect(() => {
    entriesRef.current = entries ?? [];
  }, [entries]);

  // Fetch conflicts once per identity change; cards consult this set to
  // flag rows that need review. One namespace-wide read beats N per-card calls.
  // Only when the role here is known to allow it: an unknown role sends nothing and the
  // cards simply carry no conflict chip, instead of a token-only viewer drawing an
  // AUTH_INSUFFICIENT per Memories load from this read.
  const whoami = useStore((s) => s.whoami);
  const conflictsReadable = canCall("conflicts.list", role, whoami);
  useEffect(() => {
    if (!conflictsReadable) return;
    let cancelled = false;
    // The chips need the keys, not 500 rows with values.
    fetchConflictKeys({ org: identity.org, namespace: identity.namespace })
      .then((keys) => {
        if (cancelled) return;
        setConflictKeys(new Set(keys));
      })
      .catch(() => {
        /* leave empty — cards just won't show the chip */
      });
    return () => {
      cancelled = true;
    };
  }, [identity.org, identity.namespace, conflictsReadable]);

  const flatList = useMemo(() => {
    let all = entries ?? [];
    // Fold in search hits fetched from outside the preloaded window so the
    // same filters + ranking below apply to them uniformly.
    if (searchHits != null && searchExtra.size > 0) {
      const have = new Set(all.map((e) => e.memory_key));
      const extras = [...searchExtra.values()].filter((e) => !have.has(e.memory_key));
      if (extras.length > 0) all = [...all, ...extras];
    }
    // When "Recently deleted" is on, show only tombstoned rows; otherwise
    // only live rows (defensive against polling overlap carrying both).
    if (showDeleted) {
      all = all.filter((e) => !!e.tombstoned_at);
    } else {
      all = all.filter((e) => !e.tombstoned_at);
    }
    if (pinnedOnly) all = all.filter((e) => e.pinned);
    // Tag-chip filter; case-insensitive since tags come from humans and agents.
    if (memoryTagFilter) {
      const target = memoryTagFilter.toLowerCase();
      all = all.filter((e) => (e.tags || []).some((x: string) => x.toLowerCase() === target));
    }
    // Combine store-driven filter state with the `agent:` / `tag:` query syntax.
    const effectiveAgent = queryAgent || agentFilter;
    const effectiveTags = new Set<string>(queryTags);
    if (effectiveAgent != null) all = all.filter((e) => (e.agent || "") === effectiveAgent);
    if (effectiveTags.size > 0) {
      // AND-intersection: row must carry EVERY required tag.
      all = all.filter((e) => {
        const t = new Set(e.tags || []);
        for (const required of effectiveTags) {
          if (!t.has(required)) return false;
        }
        return true;
      });
    }
    if (searchHits != null) {
      // Re-order to the server's relevance ranking; drop entries outside top_k.
      const rank = new Map(searchHits.map((k, i) => [k, i] as const));
      all = all
        .filter((e) => rank.has(e.memory_key))
        .sort((a, b) => (rank.get(a.memory_key) ?? Infinity) - (rank.get(b.memory_key) ?? Infinity));
    }
    return all;
  }, [entries, searchHits, searchExtra, pinnedOnly, showDeleted, agentFilter, queryAgent, queryTags, memoryTagFilter]);

  // Agents present in the unfiltered list (powers the chip row).
  const agentsInList = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of entries ?? []) {
      const a = e.agent || "";
      // Sentinels (*, -) aren't useful filter targets.
      if (a && !SENTINEL_AGENTS.has(a)) m.set(a, (m.get(a) || 0) + 1);
    }
    return Array.from(m.entries()).sort((a, b) => b[1] - a[1]);
  }, [entries]);

  const toggleSelect = useCallback((key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }, []);
  // One callback for every card, so the memoised cards skip a render when it is not theirs.
  const selecting = selected.size > 0;
  const openCard = useCallback(
    (e: MemoryListEntry) => (selecting ? toggleSelect(e.memory_key) : inspect(e.memory_key, { agent: e.agent })),
    [selecting, toggleSelect, inspect],
  );

  // Keyboard nav, active when no inspector is open and not typing. j / k move the focus
  // itself from card to card, as on Tasks: Enter opens the focused card (its button), Space
  // selects it (MemoryCard's onToggle), and the inspector returns focus to it on close.
  const onList = () => {
    const el = document.activeElement;
    return !el || el === document.body || !!el.closest("[data-card]");
  };
  const moveFocus = (step: number): true => {
    const cards = [...document.querySelectorAll<HTMLElement>("[data-memory-open]")];
    const here = document.activeElement?.closest("[data-card]")?.querySelector<HTMLElement>("[data-memory-open]");
    const at = here ? cards.indexOf(here) : -1;
    const next = cards[at < 0 ? 0 : Math.max(0, Math.min(cards.length - 1, at + step))];
    next?.scrollIntoView?.({ block: "nearest" });
    next?.focus({ preventScroll: true });
    return true;
  };
  useKeyboard(
    {
      j: () => moveFocus(1),
      k: () => moveFocus(-1),
      // The arrows move through the list only from a card or the page itself: on any
      // other control they are that control's (a menu trigger opens its menu), and
      // elsewhere they scroll.
      ArrowDown: () => onList() && moveFocus(1),
      ArrowUp: () => onList() && moveFocus(-1),
      // Escape clears a selection; with none it is left to whatever else listens for it.
      ...(selecting ? { Escape: () => setSelected(new Set()) } : {}),
    },
    !inspectedKey && flatList.length > 0,
  );

  // A memory's identity is (org, namespace, agent, user_id, session_id) +
  // memory_key, and this list spans agents (there is an agent filter chip). The
  // page identity carries the *session's* agent, so addressing a row by it
  // misses anything another agent wrote: reads 404 and `memory.delete` returns
  // `tombstoned: false`. The inspector already solves this by overriding agent
  // with the row's writer (App.tsx) — bulk actions need the same, per row.
  // Sentinel agents ("*", "-") are not real writers, so leave those alone.
  const identityForRow = (rowAgent: string | undefined) =>
    rowAgent && !SENTINEL_AGENTS.has(rowAgent) ? { ...identity, agent: rowAgent } : identity;

  const bulkPin = async () => {
    if (selected.size === 0 || busy) return;
    setBusy(true);
    const keys = Array.from(selected);
    let ok = 0,
      fail = 0;
    for (const k of keys) {
      const entry = flatList.find((e) => e.memory_key === k);
      if (!entry) {
        fail++;
        continue;
      }
      const rowId = identityForRow(entry.agent);
      try {
        // pinMemory needs the value; the list view doesn't carry it, so
        // refetch the row before pinning.
        const got = await getMemory(rowId, k);
        if (got && got.found) {
          // Echo char_limit if present — pinning without it silently drops
          // the cap. Read-then-write must always echo the cap.
          await pinMemory(rowId, k, got.value, {
            memoryType: got.memory_type || "semantic",
            charLimit: got.char_limit ?? undefined,
          });
          ok++;
        } else {
          fail++;
        }
      } catch {
        fail++;
      }
    }
    setBusy(false);
    setSelected(new Set());
    toast(`Pinned ${ok}${fail ? ` · ${fail} failed` : ""}`, fail ? "error" : "ok");
    bumpMemoriesVersion();
  };

  const bulkDelete = async () => {
    if (selected.size === 0 || busy) return;
    if (
      !(await confirmAction({
        title: `Delete ${selected.size} ${selected.size === 1 ? "memory" : "memories"}?`,
        message: "Tombstoned rows are restorable for 14 days from Recently deleted.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    setBusy(true);
    const keys = Array.from(selected);
    let ok = 0,
      fail = 0;
    for (const k of keys) {
      const entry = flatList.find((e) => e.memory_key === k);
      try {
        // `tombstoned: false` is a 200, not a throw — counting only the absence
        // of an exception is what made this report "Deleted N" and then show
        // every row again on refresh.
        const r = await deleteMemory(identityForRow(entry?.agent), k);
        if (r.tombstoned) ok++;
        else fail++;
      } catch {
        fail++;
      }
    }
    setBusy(false);
    setSelected(new Set());
    toast(
      ok === 0 && fail > 0
        ? `Nothing deleted — ${fail} could not be matched`
        : `Deleted ${ok}${fail ? ` · ${fail} failed` : ""}`,
      fail ? "error" : "ok",
    );
    bumpMemoriesVersion();
  };

  // Values the list response did not carry (a server predating the preview
  // field), read back in ONE memory.bulk_get for the page instead of one
  // memory.get per card.
  const [bulkValues, setBulkValues] = useState<Map<string, unknown>>(new Map());
  const fetchOnceRef = useRef<(isFirst: boolean) => Promise<void>>(async () => {});

  useAbortableEffect(
    (signal, live) => {
      setEntries(null);
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
          const lim = showDeleted ? 500 : PAGE_SIZE;
          const off = showDeleted ? 0 : page * PAGE_SIZE;
          const r = await listMemories(identity, lim, {
            include_tombstoned: showDeleted,
            offset: off,
            signal,
            known_version: isFirst ? undefined : knownVersion || undefined,
            fields: MEMORIES_LIST_FIELDS,
            previewChars: MEMORIES_PREVIEW_CHARS,
          });
          if (!live()) return;
          if (r.unchanged) return;
          knownVersion = r.namespace_version ?? "";
          setTotal(r.total);
          setTodayCount(r.today);
          // Fill before painting so a list without values never sends 100 cards to
          // their per-card fallback while the one bulk read is in flight.
          await fillMissingValues(r.entries);
          if (!live()) return;
          if (isFirst) {
            knownKeysRef.current = new Set(r.entries.map((e) => e.memory_key));
            setEntries(r.entries);
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
              // Clear the "new" highlight after 4s.
              setTimeout(() => {
                setNewKeys((prev) => {
                  const next = new Set(prev);
                  fresh.forEach((k) => next.delete(k));
                  return next;
                });
              }, 4000);
            }
          }
        } catch (e) {
          if (!live()) return;
          if (isFirst) {
            // Say so inline — an empty list that is really a failed read looked like "no
            // memories yet".
            setLoadError({ text: humanizeError(e), requestId: errorRequestId(e) });
            setEntries([]);
          }
        }
      };

      fetchOnceRef.current = fetchOnce;
      return fetchOnce(true);
    },
    [identity, page, memoriesVersion, showDeleted],
  );
  // The 8 s refresh sleeps while the tab is hidden and catches up once on
  // return; it always runs the current effect's fetchOnce, so a response
  // for a superseded namespace/page is dropped by that effect's live().
  useVisiblePoll(() => {
    void fetchOnceRef.current(false);
  }, 8000);

  // Reset to the first page when the scope changes, so we never sit on a page
  // index that no longer exists under the new filter.
  useEffect(() => {
    setPage(0);
  }, [showDeleted, identity.org, identity.namespace]);

  if (entries === null) {
    return (
      <PageContainer className="space-y-3">
        <PageHeader title="Memories" />
        <MemoryTabs active="memories" />
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
      </PageContainer>
    );
  }

  if (entries.length === 0 && loadError) {
    return (
      <PageContainer>
        <PageHeader title="Memories">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <MemoryTabs active="memories" />
        <EmptyState
          icon="warning"
          title="Couldn't load memories"
          description={loadError.requestId ? `${loadError.text} (request id ${loadError.requestId})` : loadError.text}
          action={{
            label: "Retry",
            onClick: () => {
              setLoadError(null);
              setEntries(null);
              void fetchOnceRef.current?.(true);
            },
          }}
        />
      </PageContainer>
    );
  }

  if (entries.length === 0) {
    return (
      <PageContainer>
        <PageHeader title="Memories">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <MemoryTabs active="memories" />
        <EmptyState
          icon="clock"
          title="No memories yet"
          description={
            // Keyed on auth-off, not on the name "local": the namespace an
            // auth-disabled deployment serves is configurable
            // (LOOM_DEFAULT_NAMESPACE), and this onboarding copy is about the
            // mode, not the name.
            isAuthDisabled()
              ? "This is your private single-user namespace. Anything you or your agent writes here stays — across browser sessions, container restarts, and Loom upgrades. Add a memory below, or wire up an MCP client (Settings) so your agent writes here automatically."
              : readOnly
                ? `Memories accumulate when an agent with writer access writes via MCP. ${READ_ONLY_TIP}.`
                : "Memories accumulate when your agent writes via MCP — or add one yourself to seed."
          }
          action={readOnly ? undefined : { label: "Add memory", onClick: () => setAddOpen(true) }}
        />
        <AddMemoryDialog
          open={addOpen}
          onOpenChange={setAddOpen}
          identity={identity}
          onAdded={() => bumpMemoriesVersion()}
        />
      </PageContainer>
    );
  }

  // Group entries by day label, but not while searching — day buckets
  // would hide the relevance ranking. Ranked results go under one heading.
  const searchActive = searchHits != null;
  const mountedList = firstScreenOnly ? flatList.slice(0, FIRST_SCREEN_CARDS) : flatList;
  const byDay = new Map<string, MemoryListEntry[]>();
  if (!searchActive) {
    for (const e of mountedList) {
      const label = dayLabel(e.created_at);
      const arr = byDay.get(label) || [];
      arr.push(e);
      byDay.set(label, arr);
    }
  } else {
    byDay.set("Results", mountedList);
  }
  // More rows exist server-side than we've loaded. Prefer the true total;
  // fall back to the page-cap heuristic if total wasn't returned.
  // Pagination math off the authoritative total.
  // No pagination in the deleted view — it's fetched in one shot, and the
  // namespace's live `total` (6k+) has nothing to do with the deleted count.
  const pageCount = showDeleted ? 1 : Math.max(1, Math.ceil((total || entries.length) / PAGE_SIZE));
  const rangeStart = entries.length === 0 ? 0 : page * PAGE_SIZE + 1;
  const rangeEnd = page * PAGE_SIZE + entries.length;

  return (
    <PageContainer className="pb-24">
      <PageHeader
        title="Memories"
        actions={
          <>
            <span className="text-sm text-text-muted tabular whitespace-nowrap">
              {searchActive
                ? `${flatList.length} shown`
                : showDeleted
                  ? `${flatList.length.toLocaleString()} recently deleted`
                  : `Showing ${rangeStart.toLocaleString()}–${rangeEnd.toLocaleString()} of ${(total || entries.length).toLocaleString()}`}
            </span>
            <span id={SHOWN_GROUP} className="whitespace-nowrap">
              <ButtonGroup
                aria-label="Memories shown"
                selected={showDeleted ? "deleted" : "live"}
                onClick={(value) => {
                  if ((value === "deleted") === showDeleted) return;
                  refocusShown.current = true;
                  setShowDeleted(value === "deleted");
                }}
                options={[
                  { value: "live", label: "Live" },
                  { value: "deleted", label: "Recently deleted" },
                ]}
              />
            </span>
            <Unavailable reason={readOnly ? READ_ONLY_TIP : null}>
              <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
                Add memory
              </Button>
            </Unavailable>
          </>
        }
      >
        <ScopeBadge kind="namespace" value={identity.namespace} />
      </PageHeader>
      <MemoryTabs active="memories" />

      {/* Authoritative counts (server-computed, not the truncated page). */}
      {total > 0 && !searchActive && (
        <div className="mb-5 flex flex-wrap items-center gap-2 text-sm">
          <Badge text={`${total.toLocaleString()} total`} state="neutral" />
          <Badge text={`${todayCount.toLocaleString()} today`} state="neutral" />
        </div>
      )}

      {/* How the namespace is being used, next to how many
          rows it has. A click, never on mount — the op is ~14 reads. */}
      {!searchActive && !showDeleted && <NamespaceAuditPanel identity={identity} />}

      {/* What the store would tell an agent working here —
          the planes it never touched and whether past hints were followed. */}
      {!searchActive && !showDeleted && <MentorPanel identity={identity} />}

      <AddMemoryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        identity={identity}
        onAdded={() => bumpMemoriesVersion()}
      />

      {/* Inline semantic search — filters the list in place. Press / to focus. */}
      <div className="mb-4">
        <SearchField
          ref={searchInputRef}
          value={query}
          onChange={(value) => setQuery(value)}
          placeholder="Search memories. Try a keyword, or filter with tag:work, agent:claude-desktop…"
          aria-label="Search memories"
        />
      </div>
      {/* Mounted before the first search, so a screen reader hears each summary. */}
      <div role="status" className={searchHits != null ? "-mt-2 mb-3 text-xs text-text-muted tabular" : ""}>
        {searchHits != null &&
          (searching
            ? "Searching…"
            : `${searchSummary(flatList.length, rawQuery, keywordKeys.size, keywordTotal)}${ranking ? " · finding related…" : ""}`)}
      </div>
      {!searching && searchAbstain && (
        <Alert
          role="status"
          state="warning"
          size="small"
          className="-mt-1 mb-3"
          text={
            <>
              <strong>Low confidence.</strong> No strong match for "{rawQuery}" — these are best-effort results; treat
              them as weak.
            </>
          }
        />
      )}
      {/* Active query-bar filters as dismissable badges; click removes the token. */}
      {(queryTags.length > 0 || queryAgent) && (
        <div className="-mt-2 mb-3 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-text-muted">Filtering:</span>
          {queryAgent && (
            <Chip
              label={`agent:${queryAgent}`}
              mono
              onRemove={() =>
                setQuery((q) =>
                  q
                    .replace(new RegExp(`\\bagent:${queryAgent}\\b`, "g"), "")
                    .replace(/\s+/g, " ")
                    .trim(),
                )
              }
            />
          )}
          {queryTags.map((t) => (
            <Chip
              key={t}
              label={`tag:${t}`}
              mono
              onRemove={() =>
                setQuery((q) =>
                  q
                    .replace(new RegExp(`\\btag:${t}\\b`, "g"), "")
                    .replace(/\s+/g, " ")
                    .trim(),
                )
              }
            />
          ))}
        </div>
      )}

      {(entries.some((e) => e.pinned) || agentsInList.length > 1 || showDeleted || memoryTagFilter) && (
        <div className="mb-5 flex flex-wrap items-center gap-1">
          {memoryTagFilter && (
            <Chip label={`tag:${memoryTagFilter}`} mono selected onRemove={() => setMemoryTagFilter(null)} />
          )}
          {entries.some((e) => e.pinned) && !showDeleted && (
            <Chip
              label="Pinned"
              icon={<Icon name="star" size="xs" aria-hidden="true" />}
              count={entries.filter((e) => e.pinned).length}
              selected={pinnedOnly}
              onClick={() => setPinnedOnly((v) => !v)}
            />
          )}
          {showDeleted && (
            <Chip label="Recently deleted · 14-day grace" selected onRemove={() => setShowDeleted(false)} />
          )}
          {/* Agent filter chips — only when more than one agent exists.
              Click toggles; clicking the active chip clears the filter. */}
          {agentsInList.length > 1 &&
            agentsInList.map(([agent, count]) => {
              const active = agentFilter === agent;
              return (
                <Chip
                  key={`a-${agent}`}
                  label={displayAgent(agent)}
                  count={count}
                  selected={active}
                  tip={`Show only memories written by ${displayAgent(agent)}`}
                  onClick={() => setAgentFilter(active ? null : agent)}
                />
              );
            })}
        </div>
      )}

      {selected.size > 0 && (
        <div className="sticky top-0 z-[2] mb-3 animate-fade-in flex items-center gap-2 p-2.5 rounded-xl border border-accent bg-accent-soft shadow-sm">
          <span className="text-sm font-medium text-accent tabular">{selected.size} selected</span>
          <div className="flex-1" />
          <Button type="secondary" htmlType="button" iconLeft="cross" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
          <Button type="secondary" htmlType="button" iconLeft="pin" disabled={busy} onClick={bulkPin}>
            Pin all
          </Button>
          <Button type="danger" htmlType="button" iconLeft="trash" disabled={busy} onClick={bulkDelete}>
            Delete all
          </Button>
        </div>
      )}

      {/* Filter-zeroed empty state: entries exist but filters are too
          tight. Truly-empty namespace is handled by the EmptyState branch above. */}
      {flatList.length === 0 &&
        entries.length > 0 &&
        (showDeleted ? (
          <div className="py-12 text-center">
            <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-surface-2">
              <Icon name="trash" size="md" aria-hidden="true" />
            </div>
            <h2>No recently deleted memories</h2>
            <p className="mt-1 text-sm text-text-muted leading-relaxed">
              Deleted memories stay here for 14 days so you can restore them — nothing has been deleted recently.
            </p>
            <Button type="secondary" htmlType="button" onClick={() => setShowDeleted(false)} className="mt-4">
              Back to live
            </Button>
          </div>
        ) : (
          <div className="py-12 text-center">
            <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-surface-2">
              <Icon name="clock" size="md" aria-hidden="true" />
            </div>
            <h2>Nothing matches your filters</h2>
            <p className="mt-1 text-sm text-text-muted leading-relaxed">
              {(total || entries.length).toLocaleString()} memor{(total || entries.length) === 1 ? "y is" : "ies are"}{" "}
              in this namespace, but none pass the active filters.
            </p>
            <Button
              type="secondary"
              htmlType="button"
              className="mt-4"
              onClick={() => {
                setQuery("");
                setPinnedOnly(false);
                setAgentFilter(null);
                setMemoryTagFilter(null);
              }}
            >
              Clear filters
            </Button>
          </div>
        ))}

      <div className="space-y-6">
        {Array.from(byDay.entries()).map(([day, items]) => (
          <section key={day}>
            <SectionHeading
              level={2}
              label={day}
              count={items.length}
              className="sticky top-0 z-[1] bg-bg/85 backdrop-blur py-2 -my-1"
            />
            <div className="space-y-1.5 mt-2">
              {items.map((e) => {
                const isNew = newKeys.has(e.memory_key);
                const isSelected = selected.has(e.memory_key);
                const isTombstoned = !!e.tombstoned_at;
                const isActive = inspectedKey === e.memory_key;
                const why = searchActive ? searchWhy.get(e.memory_key) : undefined;
                return (
                  <div
                    key={e.memory_key}
                    data-card=""
                    className={[
                      // The browser lays out and paints only the rows in view
                      "p-3.5 rounded-xl border flex items-start gap-3 transition-colors [content-visibility:auto] [contain-intrinsic-size:auto_112px]",
                      isSelected
                        ? "border-accent bg-surface ring-1 ring-accent/40"
                        : isActive
                          ? "border-accent bg-accent-soft"
                          : "border-border bg-surface hover:border-border-strong",
                      isNew ? "animate-fade-in ring-2 ring-accent" : "",
                      // A deleted memory keeps full-contrast text; the dashed edge and the
                      // "Deleted" line under it set it apart.
                      isTombstoned ? "border-dashed" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    <Checkbox
                      checked={isSelected}
                      onCheckedChange={() => toggleSelect(e.memory_key)}
                      onClick={(ev) => ev.stopPropagation()}
                      aria-label={`Select ${e.memory_key}`}
                      className="mt-1 shrink-0"
                    />
                    <div className="flex-1 min-w-0">
                      <MemoryCard
                        embedded
                        hasConflict={conflictKeys.has(e.memory_key)}
                        identity={identity}
                        entry={e}
                        value={e.value !== undefined ? e.value : bulkValues.get(e.memory_key)}
                        active={isActive}
                        onSelect={openCard}
                        onToggle={toggleSelect}
                        onTagClick={setMemoryTagFilter}
                        timeMode="absolute"
                      />
                      {searchActive && keywordKeys.has(e.memory_key) ? (
                        <div className="mt-1 px-2 text-xs text-text-muted" data-testid="search-why">
                          contains "{rawQuery.trim()}"
                        </div>
                      ) : (
                        why && <SearchWhy hit={why} rank={(searchHits?.indexOf(e.memory_key) ?? 0) + 1} />
                      )}
                      {isTombstoned && (
                        <div className="flex items-center justify-between mt-1 px-2 py-1 text-xs text-text-muted">
                          <span>
                            Deleted
                            {e.tombstoned_at
                              ? ` ${new Date(e.tombstoned_at).toISOString().slice(0, 16).replace("T", " ")}`
                              : ""}{" "}
                            ·{(() => {
                              if (!e.tombstoned_at) return " in grace window";
                              const ageMs = Date.now() - new Date(e.tombstoned_at).getTime();
                              const daysLeft = Math.max(0, 14 - Math.floor(ageMs / (24 * 3600 * 1000)));
                              return ` ${daysLeft} day${daysLeft === 1 ? "" : "s"} left to restore`;
                            })()}
                          </span>
                          <Button
                            type="empty"
                            htmlType="button"
                            onClick={async (ev) => {
                              ev.stopPropagation();
                              // Identity carries only (org, namespace, agent);
                              // pass user_id/session_id alongside the key.
                              const r = await restoreMemory({ ...identity, agent: e.agent || "-" }, e.memory_key, {
                                user_id: e.user_id || "-",
                                session_id: e.session_id || "-",
                              });
                              if (r.restored) {
                                toast(`Restored ${e.memory_key}`, "ok");
                                bumpMemoriesVersion();
                              } else if (r.expired) {
                                toast("Outside the 14-day grace window — can't restore", "error");
                              } else {
                                toast("Nothing to restore", "info");
                              }
                            }}
                          >
                            Restore
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      {!searchActive && pageCount > 1 && (
        <nav className="mt-8" aria-label="Pagination">
          <Pagination totalPages={pageCount} currentPage={page + 1} onChange={(n) => setPage(n - 1)} />
        </nav>
      )}
    </PageContainer>
  );
}
