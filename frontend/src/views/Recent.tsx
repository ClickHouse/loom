import { startTransition, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Checkbox } from "@clickhouse/click-ui/Checkbox";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import {
  getMemory,
  pinMemory,
  deleteMemory,
  restoreMemory,
  conflictKeys as fetchConflictKeys,
  isAuthDisabled,
  type MemoryListEntry,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { loadDockRuntime, useDock } from "../lib/assistantDock";
import { STARTERS_WAIT } from "./assistant/startersWait";
import { MemoryCard } from "../components/MemoryCard";
import { SearchScores, SearchWhy } from "../components/SearchWhy";
import { IconButton } from "../components/IconButton";
import { Alert, ListRow, LoadingSkeleton, PageHeader, SectionHeading } from "../components/ui";
import { ListPager, usePageSize } from "../components/ui/ListPager";
import { ListState } from "../components/ui/ListState";
import { ListTime } from "../components/ui/ListTime";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import { RowSkeleton } from "../components/Skeleton";
import { toast, toastErrorLine } from "../components/Toast";
import { toastCommitted } from "../lib/errorWords";
import { confirmAction } from "../components/ConfirmDialog";
import { dayLabel, restoreText, displayAgent, SENTINEL_AGENTS } from "../lib/format";
import { AddMemoryDialog } from "../flows/AddMemoryDialog";
import { DASHBOARD_AGENT, GettingStarted, useOnboarding } from "../components/GettingStarted";
import { shouldShowGettingStarted } from "../lib/onboarding";
import { PageContainer } from "../layout/PageContainer";
import { MemoryTabs } from "../components/MemoryTabs";
import { ScopeBadge } from "../components/ScopeBadge";
import { NamespaceGuideButton } from "../components/NamespaceGuideButton";
import { Unavailable } from "../components/ui";
import { READ_ONLY_TIP, canWrite, useNamespaceRole } from "../lib/namespaceRole";
import { canCall } from "../lib/canCall";
import { parseSearchQuery, type SearchQuery, takePendingSearch, toggleQueryToken } from "../lib/memoriesSearch";
import { oneOf } from "../lib/oneOf";
import { isPublicationPending } from "../lib/errors";
import { shortcutOff } from "../lib/shortcuts";
import { PHONE_QUERY, useMedia } from "../lib/useMedia";
import { useRowNav } from "../lib/useRowNav";
import { useRowSelection } from "../lib/useRowSelection";
import { sameFilter, useMemoryListRead, useMemorySearch, useSameFilter, type TagAgentFilter } from "./memoryListReads";

// The cards the first screen shows. A page of a hundred mounts them all in one task
// (395–681 ms of long tasks at 4x CPU, LCP 0.9–1.4 s); the first screen is about two
// dozen. Those mount with the page, the rest in a transition after first paint — same
// DOM, same scroll, same find-in-page once it settles.
const FIRST_SCREEN_CARDS = 12; // A screen shows eight or nine

/** What the Status select shows. Every status but Live and Recently
 *  deleted reads the newest 1,000 and keeps the matching rows: none has a server filter. */
type Status = "live" | "pinned" | "conflicted" | "verified" | "deleted";
const STATUSES: ReadonlyArray<{ value: Status; label: string }> = [
  { value: "live", label: "Live" },
  { value: "pinned", label: "Pinned" },
  { value: "conflicted", label: "Conflicted" },
  { value: "verified", label: "Verified" },
  { value: "deleted", label: "Recently deleted" },
];
const EVERY_AGENT = "*";

/** The tags and agent a parsed search filters the list by. */
function filterOf({ tags, agent }: SearchQuery): TagAgentFilter {
  return { tags, agent: agent ?? "" };
}

/** Memories: the list. What the namespace's usage says, read as a whole (the audit, the
 *  mentor, the inventory), is the assistant's starters: the header's Insights button. */
export function Recent() {
  return <MemoryList />;
}

/** Opens the assistant on its starters, nothing asked; while an answer streams the panel
 *  opens on it, and the button says when the starters show. */
function InsightsButton() {
  const working = useDock((s) => s.working);
  return (
    <>
      <Button
        type="secondary"
        htmlType="button"
        iconLeft="light-bulb"
        aria-describedby={working ? "memories-insights-wait" : undefined}
        onClick={() => void loadDockRuntime().then((m) => m.openStarters(false))}
      >
        Insights
      </Button>
      {working && (
        <span id="memories-insights-wait" className="sr-only">
          {STARTERS_WAIT}
        </span>
      )}
    </>
  );
}

function MemoryList() {
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
  const phone = useMedia(PHONE_QUERY);
  // A phone's status and agent filters wait behind the toolbar's Filters button, so the
  // first memory is inside the chrome budget.
  const [phoneFilters, setPhoneFilters] = useState(false);
  // First paint mounts the first screen; the rest follow in a transition (see
  // FIRST_SCREEN_CARDS).
  const [firstScreenOnly, setFirstScreenOnly] = useState(true);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Status>("live");
  // Surfaces tombstoned rows + a Restore action in the 14-day grace window.
  const showDeleted = status === "deleted";
  // Tag filter from the global store — set by clicking a tag chip in the
  // Inspector; it joins the search as a `tag:` token.
  const memoryTagFilter = useStore((s) => s.memoryTagFilter);
  const setMemoryTagFilter = useStore((s) => s.setMemoryTagFilter);
  // The Agent select — null means every agent. Its options are the agents seen in this
  // namespace so far, so picking one does not take the others away.
  const [agentFilter, setAgentFilter] = useState<string | null>(null);
  const [seenAgents, setSeenAgents] = useState<string[]>([]);
  // Zero-indexed page into the recency-ordered namespace list.
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = usePageSize("memories");
  const [conflictKeys, setConflictKeys] = useState<Set<string>>(new Set());
  const [addOpen, setAddOpen] = useState(false);
  const [query, setQuery] = useState("");
  // Another namespace's conflict keys and agents are not this one's: they go during render,
  // before the new namespace's first read or paint can use them (useMemorySearch drops its
  // results the same way).
  const namespaceKey = `${identity.org}/${identity.namespace}`;
  const [stateFor, setStateFor] = useState(namespaceKey);
  if (stateFor !== namespaceKey) {
    setStateFor(namespaceKey);
    setConflictKeys(new Set());
    setAgentFilter(null);
    setSeenAgents([]);
  }
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  // `/` focuses the inline search anywhere on Memories.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+/ and ⌘/ are the assistant's: only a bare "/" is the search's.
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || shortcutOff(e)) return;
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
  // grouping, the pinned star and the conflict badge stay intact (a selected row that is
  // not a hit leaves the selection).
  //
  // Query language:
  //   `tag:work`              — only memories tagged `work`
  //   `tag:work tag:urgent`   — AND-intersection (both tags)
  //   `tag:work coffee`       — semantic search for "coffee" within tag work
  //   `agent:foo bar`         — same shape for agent filter
  //
  // A tag or agent filter narrows the read on the server (every tag, in any case); the
  // list applies the same filters to a search's hits, which the server ranks unfiltered.
  const parsed = useMemo(() => parseSearchQuery(query), [query]);
  const rawQuery = parsed.text;
  // A filter covers the whole namespace, not the page on screen. A tag or agent filter is
  // asked of the server, which returns the rows carrying every tag, in any case (a server a
  // release behind matches any one tag exactly, and the list then requires every tag
  // itself). Pinned, Conflicted and Verified have no server filter: the list reads the
  // newest 1,000 and keeps the matching ones. What is typed is debounced, so `tag:…` sends
  // one read, not one per key; a chip applies at once.
  const typedNow = useSameFilter(filterOf(parsed));
  const [typed, setTyped] = useState(typedNow);
  // A tag chosen on a card or elsewhere in the app, or Clear filters, applies at once.
  const applyQuery = useCallback((next: string) => {
    setQuery(next);
    setTyped(filterOf(parseSearchQuery(next)));
  }, []);
  // A card's tag adds `tag:<tag>` to the search, or takes it out again, so tags stack the
  // way typed ones do. One callback for every card: it reads the query through a ref.
  const queryRef = useRef(query);
  queryRef.current = query;
  const toggleTag = useCallback(
    (tag: string) => applyQuery(toggleQueryToken(queryRef.current, `tag:${tag}`)),
    [applyQuery],
  );
  // A tag picked elsewhere in the app (searchByTag) joins the same way.
  useEffect(() => {
    if (!memoryTagFilter) return;
    setMemoryTagFilter(null);
    if (typedNow.tags.some((t) => t.toLowerCase() === memoryTagFilter.toLowerCase())) return;
    applyQuery(`${query} tag:${memoryTagFilter}`.trim());
  }, [memoryTagFilter, setMemoryTagFilter, query, typedNow, applyQuery]);
  // A search another surface opened the page with (an assistant card's agent).
  const navSeq = useStore((s) => s.navSeq);
  useEffect(() => {
    const pending = takePendingSearch();
    if (pending !== null) applyQuery(pending);
  }, [navSeq, applyQuery]);
  // Typing waits for a pause.
  useEffect(() => {
    const t = setTimeout(() => setTyped((prev) => (sameFilter(prev, typedNow) ? prev : typedNow)), 250);
    return () => clearTimeout(t);
  }, [typedNow]);
  const filterPending = !sameFilter(typedNow, typed);
  // The list filters by what the read was asked for, so a half-typed `tag:`/`agent:` keeps
  // the cards on screen. Built from the tags' text (a tag holds no space), so a keystroke that
  // leaves the tags as they were hands every memoised card the same set.
  const activeTagsText = typed.tags.map((t) => t.toLowerCase()).join(" ");
  const activeTags = useMemo(() => new Set(activeTagsText.split(" ").filter(Boolean)), [activeTagsText]);
  // memory.list matches an agent exactly: a typed agent the list has seen is sent as it is
  // written; one it has not (its rows all past the first page) is sent as typed, so its case
  // must match.
  const typedAgent = typed.agent
    ? (seenAgents.find((a) => a.toLowerCase() === typed.agent.toLowerCase()) ?? typed.agent)
    : "";
  const listFilter = useSameFilter({
    tags: typed.tags,
    agent: typedAgent || agentFilter || "",
    status: showDeleted ? "live" : status,
  });
  const listFiltered = listFilter.tags.length > 0 || listFilter.agent !== "" || listFilter.status !== "live";
  // The keyword half of a search reads the rows holding the word that also pass the list's
  // tag and agent filter.
  const searchFilter = useSameFilter({ tags: listFilter.tags, agent: listFilter.agent });

  // A new namespace, view or filter starts at its first page, set before the read so the
  // read never asks for a page of the old scope; `scope` counts them, for the pager's reset.
  const [pagedFor, setPagedFor] = useState({ namespaceKey, showDeleted, listFilter, scope: 0 });
  if (
    pagedFor.namespaceKey !== namespaceKey ||
    pagedFor.showDeleted !== showDeleted ||
    !sameFilter(pagedFor.listFilter, listFilter)
  ) {
    setPagedFor({ namespaceKey, showDeleted, listFilter, scope: pagedFor.scope + 1 });
    setPage(0);
  }

  const {
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
  } = useMemoryListRead({
    identity,
    filter: listFilter,
    filtered: listFiltered,
    page,
    pageSize,
    showDeleted,
    memoriesVersion,
  });
  const browsedCurrent = entriesAt.size === pageSize;

  // The first-run checklist (components/GettingStarted). It reads this list, not a second
  // one: the namespace's total, and whether an agent other than the dashboard has saved a
  // memory here — read off an unfiltered, live, first page, and kept once seen.
  const whoamiNow = useStore((s) => s.whoami);
  const mine = useStore((s) => s.myNamespaces);
  const authOff = isAuthDisabled();
  const onboarding = useOnboarding(whoamiNow?.subject ?? (authOff ? "local" : ""), namespaceKey);
  // Every checklist signal comes from one read: the unfiltered, live first page, read in this
  // namespace for the filter in force. Right after a namespace switch or a filter change the
  // rows on screen are still the last read's, and count for nothing.
  const plainRead =
    entries !== null &&
    listedIn === namespaceKey &&
    filterRead &&
    !filterPending &&
    !loadError &&
    page === 0 &&
    !listFiltered &&
    !showDeleted;
  const memoryTotal = plainRead ? (browsedTotal ?? entries.length) : null;
  const readEmpty = memoryTotal === 0;
  const agentSeen =
    plainRead && entries.some((e) => !!e.agent && e.agent !== DASHBOARD_AGENT && !SENTINEL_AGENTS.has(e.agent));
  const isNew = shouldShowGettingStarted({ mine, memoryTotal, authOff });
  const { start } = onboarding;
  useEffect(() => {
    if (isNew) start();
  }, [isNew, start]);
  const checklist =
    !readOnly &&
    entries !== null &&
    !onboarding.state.dismissed &&
    !onboarding.state.finished &&
    (isNew || onboarding.state.started);
  // Open in full while the namespace is empty, and kept open once it was, so the first
  // memory's arrival ticks the step in place; otherwise one line under the toolbar. Kept
  // per namespace: another one's checklist starts as its own.
  const [checklistOpenFor, setChecklistOpenFor] = useState<string | null>(null);
  const checklistOpen = checklistOpenFor === namespaceKey;
  useEffect(() => {
    if (!firstScreenOnly || entries === null) return;
    let raf = 0;
    raf = requestAnimationFrame(() => {
      startTransition(() => setFirstScreenOnly(false));
    });
    return () => cancelAnimationFrame(raf);
  }, [firstScreenOnly, entries]);

  const {
    searchHits,
    searchWhy,
    searchExtra,
    searchAbstain,
    keywordTotal,
    keywordKeys,
    keywordFor,
    searching,
    ranking,
  } = useMemorySearch(identity, rawQuery, searchFilter, entries, memoriesVersion);
  // Each hit's scores and "Why this result", made when the results change rather than in
  // every render: an element made in render is a new prop each time, and every memoised card
  // would render again on each keystroke.
  const { searchScores, searchAsides } = useMemo(() => {
    const scores = new Map<string, ReactNode>();
    const asides = new Map<string, ReactNode>();
    searchHits?.forEach((key, i) => {
      const hit = searchWhy.get(key);
      if (keywordKeys.has(key)) asides.set(key, <SearchWhy contains={keywordFor} />);
      else if (hit) {
        scores.set(key, <SearchScores hit={hit} />);
        asides.set(key, <SearchWhy hit={hit} rank={i + 1} />);
      }
    });
    return { searchScores: scores, searchAsides: asides };
  }, [searchHits, searchWhy, keywordKeys, keywordFor]);

  // Fetch conflicts once per identity change; cards consult this set to
  // flag rows that need review. One namespace-wide read beats N per-card calls.
  // Only when the role here is known to allow it: an unknown role sends nothing and the
  // cards simply carry no conflict chip, instead of a token-only viewer drawing an
  // AUTH_INSUFFICIENT per Memories load from this read.
  const whoami = useStore((s) => s.whoami);
  const conflictsReadable = canCall("conflicts.list", role, whoami);
  useEffect(() => {
    // A role here that cannot read conflicts, or a failed read, shows no chip (never
    // another namespace's).
    setConflictKeys(new Set());
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
    if (status === "pinned") all = all.filter((e) => e.pinned);
    else if (status === "conflicted") all = all.filter((e) => conflictKeys.has(e.memory_key));
    else if (status === "verified") all = all.filter((e) => e.verification === "human" || e.verification === "process");
    // The agent chip and the `agent:` / `tag:` query syntax. What was typed keeps its case,
    // so a typed tag or agent and the rows' compare lowercased (humans and agents both
    // write tags).
    if (typed.agent) all = all.filter((e) => (e.agent || "").toLowerCase() === typed.agent.toLowerCase());
    else if (agentFilter != null) all = all.filter((e) => (e.agent || "") === agentFilter);
    if (activeTags.size > 0) {
      // AND-intersection: row must carry EVERY required tag.
      all = all.filter((e) => {
        const t = new Set((e.tags || []).map((x) => x.toLowerCase()));
        for (const required of activeTags) {
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
  }, [entries, searchHits, searchExtra, status, conflictKeys, showDeleted, agentFilter, typed.agent, activeTags]);

  // The agents the namespace's rows were written by, gathered as pages and filters load
  // (sentinels "*" and "-" are not writers); a new namespace starts again.
  useEffect(() => {
    const here = (entries ?? []).map((e) => e.agent || "").filter((a) => a && !SENTINEL_AGENTS.has(a));
    setSeenAgents((prev) => {
      const next = [...new Set([...prev, ...here])].sort();
      return next.length === prev.length ? prev : next;
    });
  }, [entries]);

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
    // The last pin that committed with lists behind, and the last call that failed, for
    // their request ids.
    let lagging: unknown = null;
    let failure: unknown = null;
    for (const k of keys) {
      const entry = flatList.find((e) => e.memory_key === k);
      if (!entry) {
        fail++;
        continue;
      }
      const rowId = identityForRow(entry.agent);
      try {
        // pinMemory writes the whole row back; the list view doesn't carry it, so
        // refetch the row before pinning, by its own user and session.
        const readId = { ...rowId, user_id: entry.user_id || "-", session_id: entry.session_id || "-" };
        const got = await getMemory(readId, k);
        if (got && got.found) {
          await pinMemory(rowId, k, got);
          ok++;
        } else {
          fail++;
        }
      } catch (e) {
        // Pinned, with only lists and search behind: counted as pinned, and said once below.
        if (isPublicationPending(e)) {
          ok++;
          lagging = e;
        } else {
          fail++;
          failure = e;
        }
      }
    }
    setBusy(false);
    selection.clear();
    const behind = lagging ? " · lists may show the old state for a moment" : "";
    const line = `Pinned ${ok}${fail ? ` · ${fail} failed` : ""}${behind}`;
    if (fail) toastErrorLine(line, failure);
    else if (lagging) toastCommitted(lagging, line);
    else toast(line, "ok");
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
    selection.clear();
    toast(
      ok === 0 && fail > 0
        ? `Nothing deleted — ${fail} could not be matched`
        : `Deleted ${ok}${fail ? ` · ${fail} failed` : ""}`,
      fail ? "error" : "ok",
    );
    bumpMemoriesVersion();
  };

  // A filtered list is one read of its newest 1,000 matches, paged here.
  const searchActive = searchHits != null;
  const pageList = useMemo(
    () => (listFiltered && !searchActive ? flatList.slice(page * pageSize, (page + 1) * pageSize) : flatList),
    [flatList, listFiltered, searchActive, page, pageSize],
  );
  const pageKeys = useMemo(() => pageList.map((e) => e.memory_key), [pageList]);
  // Checkboxes: one row, a Shift-click range, every row on the page; Escape clears them
  // while the drawer is closed (it owns Escape while open).
  const selection = useRowSelection(pageKeys, { enabled: !inspectedKey });
  const { selected, toggle: toggleSelect } = selection;
  // One callback for every card, so the memoised cards skip a render when it is not theirs.
  const selecting = selected.size > 0;
  const openCard = useCallback(
    (e: MemoryListEntry) => (selecting ? toggleSelect(e.memory_key) : inspect(e.memory_key, { agent: e.agent })),
    [selecting, toggleSelect, inspect],
  );

  // Keyboard nav, active when no inspector is open and not typing: the list is one tab
  // stop, j / k and the arrows move the focus from card to card, Enter opens the focused
  // card (its button), Space selects it, and the inspector returns focus to it on close.
  const listNav = useRowNav({ enabled: !inspectedKey && flatList.length > 0 });

  // The namespace is empty only when the read worked and no filter narrowed it; a filter
  // that matches nothing keeps the toolbar and says so below.
  const failed = entries !== null && entries.length === 0 && !!loadError;
  const emptyNamespace =
    entries !== null && entries.length === 0 && !loadError && !listFiltered && !filterPending && !showDeleted;
  useEffect(() => {
    if (readEmpty && checklist) setChecklistOpenFor(namespaceKey);
  }, [readEmpty, checklist, namespaceKey]);

  // Group entries by day label, but not while searching — day buckets would hide the
  // relevance ranking.
  // A tag or an agent narrows any status; a status but Live is a filter too.
  // Built from the debounced filter the cards follow, so a half-typed `tag:` changes none of it.
  const narrowed = typed.tags.length > 0 || !!typed.agent || agentFilter != null;
  const filtering = narrowed || status !== "live";
  // The filters in force, as they read in the toolbar ("tag:api, agent:bot, Pinned").
  const filterNames = [
    ...typed.tags.map((t) => `tag:${t}`),
    ...(typed.agent ? [`agent:${typed.agent}`] : agentFilter != null ? [displayAgent(agentFilter)] : []),
    ...(status !== "live" ? [STATUSES.find((o) => o.value === status)?.label ?? status] : []),
  ].join(", ");
  // What the reader typed, for the no-match row. With a select in force too (agent, a
  // status but Live) the row says "the filters in force", not blame the words alone.
  const typedWords =
    agentFilter != null || status !== "live"
      ? ""
      : [
          searchActive ? keywordFor : "",
          ...typed.tags.map((t) => `tag:${t}`),
          typed.agent ? `agent:${typed.agent}` : "",
        ]
          .filter(Boolean)
          .join(", ");
  const clearFilters = () => {
    // Every Clear leaves with the filters it clears: focus goes to the search.
    searchInputRef.current?.focus();
    applyQuery(rawQuery);
    setStatus("live");
    setAgentFilter(null);
  };
  const mountedList = firstScreenOnly ? pageList.slice(0, FIRST_SCREEN_CARDS) : pageList;
  const byDay = new Map<string, MemoryListEntry[]>();
  for (const e of mountedList) {
    const label = searchActive ? "" : dayLabel(e.created_at);
    const arr = byDay.get(label) || [];
    arr.push(e);
    byDay.set(label, arr);
  }
  // The deleted view is fetched in one shot: the namespace's live total has nothing to do
  // with the deleted count. A filtered list counts its matches.
  const listTotal = showDeleted || listFiltered ? flatList.length : (browsedTotal ?? entries?.length ?? 0);
  const rangeStart = pageList.length === 0 ? 0 : (showDeleted ? 0 : page * pageSize) + 1;
  const rangeEnd = (showDeleted ? 0 : page * pageSize) + pageList.length;
  // One count, at the end of the toolbar: the range while browsing, the
  // matches for a search or a filter, the last one while a read is in flight, nothing on an
  // error or before the first read.
  const matches = (n: number) => `${n.toLocaleString()} match${n === 1 ? "" : "es"}`;
  const countText =
    entries === null || failed
      ? ""
      : searchActive
        ? // What a search covers: the newest rows holding the word, when there are more.
          `${matches(flatList.length)}${keywordTotal > keywordKeys.size ? ` · the newest ${keywordKeys.size.toLocaleString()} of ${keywordTotal.toLocaleString()} holding "${keywordFor}"` : ""}${ranking ? " · finding related…" : ""}`
        : showDeleted || listFiltered
          ? // A tag or agent filter is matched on the server, whose total is exact; a status
            // is kept here among the rows read.
            matches(
              // Of the filter it read: not of a typed tag still on its way.
              !showDeleted && listFilter.status === "live" && serverMatches !== null && filterRead && !filterPending
                ? serverMatches
                : flatList.length,
            )
          : `${rangeStart.toLocaleString()}–${rangeEnd.toLocaleString()} of ${listTotal.toLocaleString()}`;
  const lastCount = useRef("");
  if (countText && !(searching || refreshing)) lastCount.current = countText;
  const countShown = searching || refreshing ? lastCount.current : countText;
  const busyList = refreshing || searching || ranking;

  const statusSelect = (className: string) => (
    <Select
      value={status}
      onSelect={(value) => {
        const v = oneOf(
          STATUSES.map((o) => o.value),
          value,
        );
        if (v !== null) setStatus(v);
      }}
      triggerProps={{ "aria-label": "Status" }}
      className={className}
    >
      {STATUSES.filter((o) => o.value !== "conflicted" || conflictsReadable).map((o) => (
        <Select.Item key={o.value} value={o.value}>
          {o.label}
        </Select.Item>
      ))}
    </Select>
  );
  const agentSelect = (className: string) =>
    (seenAgents.length > 1 || agentFilter != null) && (
      <Select
        value={agentFilter ?? EVERY_AGENT}
        onSelect={(value) => setAgentFilter(value === EVERY_AGENT ? null : value)}
        triggerProps={{ "aria-label": "Agent" }}
        className={className}
      >
        <Select.Item value={EVERY_AGENT}>Every agent</Select.Item>
        {seenAgents.map((a) => (
          <Select.Item key={a} value={a}>
            {displayAgent(a)}
          </Select.Item>
        ))}
      </Select>
    );

  return (
    <PageContainer className="pb-24">
      <PageHeader
        title="Memories"
        actions={
          <>
            {/* Not on a phone, where the title row keeps the first memory inside the first
                screen: there the header's Assistant button opens the panel, which shows the
                starters on an empty conversation (New conversation starts one). */}
            {!phone && <InsightsButton />}
            <NamespaceGuideButton identity={identity} compact={phone} />
            {/* The empty state's own Add is the one primary while it shows. */}
            {!emptyNamespace && (
              <Unavailable reason={readOnly ? READ_ONLY_TIP : null}>
                <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setAddOpen(true)}>
                  Add memory
                </Button>
              </Unavailable>
            )}
          </>
        }
      >
        {/* The phone's top bar names the namespace; the title row keeps its actions beside it. */}
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>
      <MemoryTabs active="memories" />

      <AddMemoryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        identity={identity}
        onAdded={() => bumpMemoriesVersion()}
      />

      {/* The search, the filters and where the list stands, in the one bar that stays at
          the top as the list scrolls, in every state: nothing moves when the rows land. A
          filter picked from a card further down is seen to apply. While memories are
          selected, the bar holds their actions instead. */}
      <ListToolbar
        label="Search and filters"
        count={countShown && <span aria-busy={busyList || undefined}>{countShown}</span>}
        selection={
          selecting
            ? {
                count: selected.size,
                onClear: selection.clear,
                all: { total: pageKeys.length, checked: selection.allSelected, onToggle: selection.toggleAll },
                actions: [
                  { label: "Pin", icon: "pin", disabled: busy, onClick: bulkPin },
                  {
                    label: "Delete",
                    icon: "trash",
                    danger: true,
                    disabled: busy,
                    onClick: bulkDelete,
                  },
                ],
              }
            : null
        }
      >
        <ListToolbarSearch>
          <SearchField
            ref={searchInputRef}
            value={query}
            onChange={(value) => setQuery(value)}
            // A phone's field holds the short form only.
            placeholder={
              phone
                ? "Search, or tag:work"
                : "Search memories. Try a keyword, or filter with tag:work, agent:claude-desktop…"
            }
            aria-label="Search memories"
          />
        </ListToolbarSearch>
        <span className="sm:hidden">
          <IconButton
            icon="filter"
            // Filled (Click UI's secondary) while a status or an agent is picked.
            type={status !== "live" || agentFilter != null ? "secondary" : "ghost"}
            label={status !== "live" || agentFilter != null ? "Filters, some in force" : "Filters"}
            tooltip="Status and agent"
            aria-expanded={phoneFilters}
            onClick={() => setPhoneFilters((v) => !v)}
          />
        </span>
        {statusSelect("hidden w-44 shrink-0 sm:block")}
        {agentSelect("hidden w-44 shrink-0 sm:block")}
        {filtering && (
          <Button type="empty" htmlType="button" className="hidden shrink-0 sm:inline-flex" onClick={clearFilters}>
            Clear filters
          </Button>
        )}
      </ListToolbar>
      {/* A phone's toolbar has room for the search only; the filters open under it. */}
      {phoneFilters && (
        <div className="-mt-1 mb-3 flex items-center gap-2 sm:hidden">
          {statusSelect("min-w-0 flex-1")}
          {agentSelect("min-w-0 flex-1")}
          {filtering && (
            <Button type="empty" htmlType="button" onClick={clearFilters} aria-label="Clear filters">
              Clear
            </Button>
          )}
        </div>
      )}
      {!searching && searchAbstain && (
        <Alert
          role="status"
          state="warning"
          size="small"
          className="mb-3"
          text={
            <>
              <strong>Low confidence.</strong> No strong match for "{keywordFor}" — these are best-effort results; treat
              them as weak.
            </>
          }
        />
      )}

      {checklist && (
        <GettingStarted
          // A token form opened, or a token followed, in one namespace is not another's.
          key={namespaceKey}
          onboarding={onboarding}
          identity={identity}
          agentSeen={agentSeen}
          // The live, unfiltered count: a deleted memory is not a first memory.
          hasMemory={(memoryTotal ?? 0) > 0}
          authOff={authOff}
          collapsed={!readEmpty && !checklistOpen}
          onExpand={() => setChecklistOpenFor(namespaceKey)}
          onMemoryAdded={() => bumpMemoriesVersion()}
          onHidden={() => searchInputRef.current?.focus()}
        />
      )}

      {entries === null && (
        <LoadingSkeleton label="Loading memories">
          <div className="space-y-2">
            <RowSkeleton kind="memory" />
            <RowSkeleton kind="memory" />
            <RowSkeleton kind="memory" />
          </div>
        </LoadingSkeleton>
      )}

      {failed && (
        <ListState
          kind="error"
          things="memories"
          error={loadError.text}
          requestId={loadError.requestId || undefined}
          onRetry={retry}
        />
      )}

      {emptyNamespace && !checklist && (
        <ListState
          kind="empty"
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
      )}

      {/* Rows exist, or a filter was asked of the server, and none pass: one line and a way
          out, once the read for the filter in force has landed. */}
      {entries !== null &&
        !failed &&
        !emptyNamespace &&
        flatList.length === 0 &&
        !searching &&
        filterRead &&
        !filterPending &&
        (showDeleted && !narrowed ? null : (
          <ListState
            kind="filtered"
            things="memories"
            filter={typedWords || undefined}
            onClear={() => {
              // After clearFilters, which keeps the typed words: this row clears them too.
              clearFilters();
              setQuery("");
            }}
          />
        ))}
      {entries !== null && !failed && showDeleted && flatList.length === 0 && !narrowed && filterRead && (
        <ListState
          kind="empty"
          icon="trash"
          title="No recently deleted memories"
          description="Deleted memories stay here for 14 days so you can restore them — nothing has been deleted recently."
          action={{ label: "Show live memories", onClick: () => setStatus("live") }}
        />
      )}

      {truncated && (
        <p className="mb-3 text-xs text-text-muted">
          {listFilter.tags.length > 0 || listFilter.agent
            ? "Showing the newest matches. Add a tag or a search word to narrow them."
            : `Showing the ${filterNames.toLowerCase()} memories among the newest ones read.`}
        </p>
      )}
      {/* Where a page change takes the reader: a heading a screen reader announces. */}
      <h2 id="memories-list-top" tabIndex={-1} className="sr-only focus:not-sr-only focus:mb-2 focus:text-sm">
        Memories, page {page + 1}
      </h2>
      <div ref={listNav} className="space-y-6" aria-busy={refreshing || undefined}>
        {Array.from(byDay.entries()).map(([day, items]) => (
          <section key={day || "results"} aria-label={day ? undefined : "Search results"}>
            {day && <SectionHeading level={2} label={day} count={items.length} />}
            <div className={`space-y-2 ${day ? "mt-2" : ""}`}>
              {items.map((e) => {
                const isNew = newKeys.has(e.memory_key);
                const isSelected = selected.has(e.memory_key);
                const isTombstoned = !!e.tombstoned_at;
                const isActive = inspectedKey === e.memory_key;
                return (
                  <ListRow
                    key={e.memory_key}
                    data-card=""
                    // Back to this page focuses the card that had the focus.
                    data-focus-key={`memory:${e.memory_key}`}
                    checked={isSelected}
                    active={isActive}
                    deleted={isTombstoned}
                    className={isNew ? "animate-fade-in" : ""}
                    leading={
                      <Checkbox
                        checked={isSelected}
                        onClick={(ev) => {
                          ev.stopPropagation();
                          toggleSelect(e.memory_key, ev.shiftKey);
                        }}
                        aria-label={`Select ${e.memory_key}`}
                        className="list-row-control"
                      />
                    }
                  >
                    <MemoryCard
                      embedded
                      hasConflict={conflictKeys.has(e.memory_key)}
                      identity={identity}
                      entry={e}
                      value={e.value !== undefined ? e.value : bulkValues.get(e.memory_key)}
                      active={isActive}
                      onSelect={openCard}
                      onTagClick={toggleTag}
                      activeTags={activeTags}
                      meta={searchScores.get(e.memory_key)}
                      aside={searchAsides.get(e.memory_key)}
                    />
                    {isTombstoned && (
                      <div className="flex items-center justify-between mt-1 py-1 text-xs text-text-muted">
                        <span>
                          Deleted <ListTime time={e.tombstoned_at} quiet /> · {restoreText(e.tombstoned_at)}
                        </span>
                        <Button
                          type="empty"
                          htmlType="button"
                          className="list-row-control"
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
                  </ListRow>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      {/* A filtered list is paged here, so its page is the page asked for; a browsed one is
          read a page at a time, so the pager pages from the rows' page. */}
      {!searchActive && !showDeleted && !failed && entries !== null && (
        <ListPager
          label="Memories pages"
          page={!listFiltered && browsedCurrent ? entriesAt.page : page}
          busy={refreshing}
          resetKey={String(pagedFor.scope)}
          pageSize={pageSize}
          shown={(listFiltered && filterRead) || (!listFiltered && browsedCurrent) ? pageList.length : 0}
          total={listFiltered ? (filterRead ? listTotal : undefined) : browsedTotal}
          // A tag or agent read the server cut short holds its newest matches, not every one.
          // A status is kept among the rows read, which the note under the toolbar says.
          capped={truncated && listFilter.status === "live" && (listFilter.tags.length > 0 || listFilter.agent !== "")}
          onPage={setPage}
          onPageSize={(n) => {
            setPageSize(n);
            setPage(0);
          }}
          topId="memories-list-top"
        />
      )}
    </PageContainer>
  );
}
