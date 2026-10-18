import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Switch } from "@clickhouse/click-ui/Switch";
import {
  searchConversations,
  listConversationThreads,
  getNamespace,
  setConversationLogging,
  type ConversationHit,
  type ConversationThreadSummary,
} from "../api";
import { setViewParam, useStore, useIdentity } from "../lib/state";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { Skeleton } from "../components/Skeleton";
import { ListRow, LoadingSkeleton, PageHeader, SectionHeading } from "../components/ui";
import { ListPager } from "../components/ui/ListPager";
import { ListState } from "../components/ui/ListState";
import { ListTime } from "../components/ui/ListTime";
import { ListToolbar } from "../components/ui/ListToolbar";
import { toast } from "../components/Toast";
import { AgentBadge } from "../components/AgentBadge";
import { displayAgent, truncate, humanizeError } from "../lib/format";
import { useAbortableEffect, useKeyboard } from "../lib/hooks";
import { useRowNav } from "../lib/useRowNav";
import { IconButton } from "../components/IconButton";
import { SessionBriefsSection } from "../components/SessionBriefsSection";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { ThreadReplayDialog, threadTitle, UNTITLED_THREAD, type ReplayThread } from "../components/ThreadReplayDialog";

const THREADS_PAGE = 30;

// The fields the Sessions list reads of a thread.
const THREADS_FIELDS = ["thread_id", "agent", "msg_count", "title", "last_role", "last_seen", "last_content_excerpt"];
// The hit lines a thread's search row shows; its match count says how many it has.
const HIT_LINES = 3;

/** A listed thread's name: its title, else the user's message the list carries (its last
 *  turn, when that is the user's), else "" ("Untitled thread"). The list has no first
 *  user message; the replay names the thread by it once its turns are read. */
function listedTitle(t: ConversationThreadSummary): string {
  return threadTitle(t.title, t.last_role === "user" ? t.last_content_excerpt : "");
}

/** `text` with each word of the query that it holds in 600 weight: Click UI has no
 *  highlight colour, so a match is set in bold, never coloured. */
function emphasize(text: string, query: string) {
  const words = query
    .trim()
    .split(/\s+/)
    .filter((w) => w.length > 1)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (words.length === 0) return text;
  const re = new RegExp(`(${words.join("|")})`, "gi");
  return text.split(re).map((part, i) =>
    i % 2 === 1 ? (
      <strong key={i} className="font-semibold">
        {part}
      </strong>
    ) : (
      part
    ),
  );
}

export function Conversations() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const roleHere = useNamespaceRole(identity.namespace);
  const pendingConversationThread = useStore((s) => s.pendingConversationThread);
  const clearPendingConversationThread = useStore((s) => s.clearPendingConversationThread);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<ConversationHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  // Filter the namespace-wide thread list down to one agent (claude-code,
  // claude-desktop, …). Threads are scoped to the namespace; an agent filter
  // keeps you from staring at every tool's sessions at once.
  const [agentFilter, setAgentFilter] = useState("*");
  // Per-namespace "record my conversations" switch (null = not yet loaded).
  const [loggingEnabled, setLoggingEnabled] = useState<boolean | null>(null);
  // Recent threads landing — populated on mount so a user with no
  // query sees what's there instead of a dead-end empty state. Also
  // shown beneath a "no matches" search result so users have a
  // recovery path instead of just "try a different phrase."
  const [threads, setThreads] = useState<ConversationThreadSummary[] | null>(null);
  // Thread-list read failure, shown inline with a Retry.
  const [threadsError, setThreadsError] = useState<unknown>(null);
  // A failed search: said with a Retry, never as "no matches".
  const [searchError, setSearchError] = useState<unknown>(null);
  // The query the hits on screen answer, for the matched words' weight.
  const [hitsFor, setHitsFor] = useState("");
  const [threadsRetry, setThreadsRetry] = useState(0);
  // Threads come a page at a time, most recently active first, over the past year.
  const [threadPage, setThreadPage] = useState(0);
  const [threadsPaged, setThreadsPaged] = useState(true);
  // Known only once a page after the last full one comes back empty.
  const [threadsTotal, setThreadsTotal] = useState<number | undefined>(undefined);
  const [seenAgents, setSeenAgents] = useState<string[]>([]);
  // The first page's threads: a later page that repeats them came from a build that ignores
  // `offset`, even if a thread moved to the top between the reads.
  const firstPageRef = useRef<Set<string>>(new Set());
  // Selected thread for the message-replay dialog. Click a thread
  // card OR a search-hit thread header → the dialog reads the full messages and shows them.
  // `knownAgent`: the agent the opener named, for a replica of the previous release.
  const [openThread, setOpenThread] = useState<ReplayThread | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  // Bumped per search and per namespace switch: only the latest search writes hits.
  const searchSeq = useRef(0);
  // Bumped per namespace switch: a logging write that fails after the switch must not
  // revert the new namespace's switch.
  const scopeGen = useRef(0);

  // `/` focuses search anywhere on Conversations (not while typing or inside the
  // thread dialog). Same shortcut as Recall/Recent for muscle-memory consistency
  // across the dashboard.
  const keys = useMemo(
    () => ({
      "/": () => {
        searchRef.current?.focus();
        searchRef.current?.select();
      },
    }),
    [],
  );
  useKeyboard(keys);

  // Hits, threads, the agent filter and the recording switch belong to the namespace
  // they were read for; a switch clears them so B never shows A's rows or toggles A's
  // value into B.
  useEffect(() => {
    searchSeq.current += 1;
    scopeGen.current += 1;
    setHits(null);
    setBusy(false);
    setThreads(null);
    setThreadPage(0);
    setSeenAgents([]);
    setThreadsPaged(true);
    setThreadsTotal(undefined);
    setAgentFilter("*");
    setLoggingEnabled(null);
  }, [identity.org, identity.namespace]);

  useAbortableEffect(
    (signal, live) => {
      setThreadsError(null);
      listConversationThreads(
        { org: identity.org, namespace: identity.namespace },
        {
          limit: THREADS_PAGE,
          offset: threadPage * THREADS_PAGE,
          window_days: 365,
          agent: agentFilter,
          signal,
          fields: THREADS_FIELDS,
        },
      )
        .then((r) => {
          if (!live()) return;
          const rows = r.threads || [];
          // A build that predates `offset` answers the first page again: stay on it and
          // offer no further page rather than show the same threads twice.
          if (threadPage === 0) firstPageRef.current = new Set(rows.map((t) => t.thread_id));
          else if (rows.length > 0 && rows.every((t) => firstPageRef.current.has(t.thread_id))) {
            setThreadsPaged(false);
            setThreadPage(0);
            return;
          } else if (rows.length === 0) {
            // The last page was full, so the one after it is empty: go back to it, now
            // knowing how many threads there are.
            setThreadsTotal(threadPage * THREADS_PAGE);
            setThreadPage(threadPage - 1);
            return;
          }
          setThreads(rows);
          setSeenAgents((prev) => Array.from(new Set([...prev, ...rows.map((t) => t.agent)])).sort());
        })
        .catch((e) => {
          if (!live()) return;
          // An empty list that is really a failed read looked like "no
          // conversations yet".
          setThreadsError(e);
          setThreads([]);
        });
    },
    [identity.org, identity.namespace, threadsRetry, threadPage, agentFilter],
  );
  // The recording switch is the namespace's, read once per namespace, not per page.
  useAbortableEffect(
    (signal, live) => {
      getNamespace(identity, { signal })
        .then((n) => {
          if (live()) setLoggingEnabled(n?.conversation_logging_enabled ?? true);
        })
        .catch(() => {
          if (live()) setLoggingEnabled(null);
        });
    },
    [identity.org, identity.namespace],
  );

  const run = async () => {
    if (!q.trim()) return;
    const seq = ++searchSeq.current;
    setBusy(true);
    setSearchError(null);
    try {
      const r = await searchConversations(identity, q, { top_k: 50, agent: agentFilter });
      if (seq !== searchSeq.current) return;
      setHits(r.hits || []);
      setHitsFor(q);
    } catch (e) {
      if (seq !== searchSeq.current) return;
      setSearchError(e);
      setHits(null);
    } finally {
      if (seq === searchSeq.current) setBusy(false);
    }
  };

  // Group search hits by thread so multiple matches in the same
  // conversation collapse to one card with a hit count, not N
  // duplicates. Within a thread, hits stay in server order (relevance).
  // A search spans the caller's namespaces, so a hit opens in its own one.
  // Hits carry no agent; the recent-threads list names it when it holds
  // the thread.
  const groupedHits = useMemo(() => {
    if (!hits) return null;
    const byThread = new Map<string, ConversationHit[]>();
    for (const h of hits) {
      const key = `${h.namespace || identity.namespace}/${h.thread_id}`;
      const arr = byThread.get(key) || [];
      arr.push(h);
      byThread.set(key, arr);
    }
    const listed = new Map((threads || []).map((t) => [t.thread_id, t] as const));
    return Array.from(byThread.entries()).map(([key, items]) => {
      const { thread_id } = items[0];
      const namespace = items[0].namespace || identity.namespace;
      const known = namespace === identity.namespace ? listed.get(thread_id) : undefined;
      // Named as the list names it, else by the earliest thing the user said among the hits.
      const firstUser = [...items].sort((a, b) => a.ts.localeCompare(b.ts)).find((h) => h.role === "user");
      const title = (known && listedTitle(known)) || threadTitle("", firstUser?.content_excerpt);
      const newest = items.reduce((a, b) => (b.ts > a.ts ? b : a)).ts;
      return { key, thread_id, namespace, agent: known?.agent, title, newest, hits: items };
    });
  }, [hits, threads, identity.namespace]);

  const matchCount =
    groupedHits === null
      ? ""
      : `${hits?.length} match${hits?.length === 1 ? "" : "es"} in ${groupedHits.length} thread${groupedHits.length === 1 ? "" : "s"}`;
  // The toolbar's status region says the count; this one says a search is under way.
  const searchStatus = busy ? "Searching…" : "";

  // The open thread is the page's URL (`?thread=`): a reload or Back reopens it.
  const openThreadDialog = useCallback(
    (
      thread_id: string,
      opts: { namespace?: string; hit?: ConversationHit; knownAgent?: string; knownTitle?: string } = {},
    ) => {
      setViewParam("thread", thread_id);
      setOpenThread({
        thread_id,
        namespace: opts.namespace || identity.namespace,
        knownAgent: opts.knownAgent,
        hit: opts.hit,
        knownTitle: opts.knownTitle,
      });
    },
    [identity.namespace],
  );

  const closeThreadDialog = () => {
    setOpenThread(null);
    setViewParam("thread", null);
  };

  // Deep-link from a memory's "View source conversation" → open that thread.
  useEffect(() => {
    if (!pendingConversationThread) return;
    const { threadId, agent } = pendingConversationThread;
    void openThreadDialog(threadId, { knownAgent: agent || undefined });
    clearPendingConversationThread();
  }, [pendingConversationThread, openThreadDialog, clearPendingConversationThread]);

  const canSetLogging = canCall("conversation.set_logging", roleHere, whoami);

  const toggleLogging = async () => {
    if (loggingEnabled === null) return;
    const next = !loggingEnabled;
    const gen = scopeGen.current;
    setLoggingEnabled(next); // optimistic
    try {
      await setConversationLogging(identity, next);
      toast(next ? "Conversation recording on" : "Recording off — new turns won't be stored", "ok");
    } catch (e) {
      if (gen === scopeGen.current) setLoggingEnabled(!next); // revert
      toast(`Couldn't update: ${humanizeError(e)}`, "error");
    }
  };

  // One count, at the end of the toolbar: the matches of a search, the
  // threads shown (the total is known only once the last page is read), the last one while
  // a read is in flight, nothing on an error.
  const pageStart = threadPage * THREADS_PAGE;
  const countText =
    hits !== null
      ? matchCount
      : threadsError || !threads || threads.length === 0
        ? ""
        : threadsTotal !== undefined
          ? `${(pageStart + 1).toLocaleString()}–${(pageStart + threads.length).toLocaleString()} of ${threadsTotal.toLocaleString()}`
          : `${threads.length.toLocaleString()} shown`;
  const loadingThreads = threads === null;
  const lastCount = useRef("");
  if (countText && !busy && !loadingThreads) lastCount.current = countText;
  const countShown = busy || (loadingThreads && hits === null) ? lastCount.current : countText;

  // One tab stop over each list's rows: arrows and j/k move, Enter opens.
  const threadsNav = useRowNav({ enabled: openThread === null && !!threads?.length });
  const hitsNav = useRowNav({ enabled: openThread === null && !!groupedHits?.length });

  const agentSelect = (className: string) =>
    seenAgents.length > 1 && (
      <Select
        value={agentFilter}
        onSelect={(value) => {
          setAgentFilter(value);
          setThreadsTotal(undefined);
          setThreadPage(0);
        }}
        triggerProps={{ "aria-label": "Agent" }}
        className={className}
      >
        <Select.Item value="*">Every agent</Select.Item>
        {seenAgents.map((a) => (
          <Select.Item key={a} value={a}>
            {displayAgent(a)}
          </Select.Item>
        ))}
      </Select>
    );

  const renderRecentThreads = (label = "Recent threads") => {
    if (!threads || threads.length === 0) return null;
    return (
      <div className="space-y-2">
        <div id="threads-list" tabIndex={-1} className="outline-none">
          <SectionHeading level={2} label={label} caption="active in the last year" />
        </div>
        <div ref={threadsNav} className="space-y-2">
          {threads.map((t) => {
            const title = listedTitle(t);
            return (
              <ListRow key={t.thread_id} data-thread-row="">
                <button
                  type="button"
                  data-row-open=""
                  onClick={() =>
                    openThreadDialog(t.thread_id, { knownAgent: t.agent, knownTitle: title || UNTITLED_THREAD })
                  }
                  className="block w-full text-left bg-transparent border-0 p-0 cursor-pointer after:absolute after:inset-0 after:content-['']"
                >
                  {/* Line 1: the thread; right, when it was last active. */}
                  <span className="flex items-baseline gap-3">
                    <span
                      className={`min-w-0 flex-1 truncate text-sm font-medium ${title ? "text-text" : "text-text-muted"}`}
                    >
                      {title || UNTITLED_THREAD}
                    </span>
                    <span className="shrink-0 text-xs text-text-muted">
                      <ListTime iso={t.last_seen} quiet />
                    </span>
                  </span>
                  {/* Line 2: the last turn; right, who and how long. Under 640 px the right
                      part takes its own line. */}
                  <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-muted">
                    <span className="flex min-w-0 flex-1 basis-full items-center gap-2 sm:basis-0">
                      <span className="inline-flex shrink-0">
                        <Badge text={t.last_role} state="neutral" size="sm" />
                      </span>{" "}
                      <span className="min-w-0 flex-1 truncate">{truncate(t.last_content_excerpt, 140)}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2 tabular">
                      {t.agent && <AgentBadge agent={t.agent} quiet />}
                      <span>
                        {t.msg_count.toLocaleString()} message{t.msg_count === 1 ? "" : "s"}
                      </span>
                    </span>
                  </span>
                </button>
              </ListRow>
            );
          })}
        </div>
        {threadsPaged && hits === null && (
          <ListPager
            label="Thread pages"
            page={threadPage}
            pageSize={THREADS_PAGE}
            shown={threads.length}
            total={threadsTotal}
            onPage={setThreadPage}
            topId="threads-list"
          />
        )}
      </div>
    );
  };

  const recording = canSetLogging && loggingEnabled !== null && (
    <span className="flex items-center gap-1">
      <Switch
        checked={loggingEnabled}
        onCheckedChange={toggleLogging}
        // "Record" on a phone, beside the title, so the switch keeps to the title's row.
        // Click UI names the switch with the label as a string, so the name is given here.
        label={
          <>
            Record<span className="max-sm:sr-only"> conversations</span>
          </>
        }
        aria-label="Record conversations"
        orientation="horizontal"
        dir="start"
      />
      <IconButton
        icon="info-in-circle"
        size="xs"
        label="About recording"
        tooltip="When off, new conversation turns are not stored for this namespace"
      />
    </span>
  );

  return (
    <PageContainer>
      {/* Recording is the namespace's setting, not a filter: it sits with the title. */}
      <PageHeader title="Conversations" actions={recording || undefined}>
        {/* The phone's top bar names the namespace; the title row keeps its action beside it. */}
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>
      {/* Left to the title on a phone, where the threads need the room. */}
      <div className="mb-4 hidden items-center gap-1 text-sm text-text-muted sm:flex">
        The raw chats your agents recorded.
        <IconButton
          icon="info-in-circle"
          size="xs"
          label="About conversations"
          tooltip="Search to find what was said; open a thread to replay the full exchange."
        />
      </div>

      {/* The search and the agent filter, in the bar that stays at the top as the threads
          scroll. */}
      <ListToolbar
        label="Search and filters"
        count={countShown && <span aria-busy={busy || loadingThreads || undefined}>{countShown}</span>}
      >
        <div className="min-w-[10rem] flex-1">
          <SearchField
            ref={searchRef}
            value={q}
            onChange={(value) => {
              setQ(value);
              if (!value) {
                setHits(null);
                setSearchError(null);
              }
            }}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder="Search transcripts"
            aria-label="Search conversation transcripts"
          />
        </div>
        {agentSelect("hidden w-48 shrink-0 sm:block")}
      </ListToolbar>
      {/* A phone's bar has room for the search and the count only; the agent filter sits under it. */}
      {seenAgents.length > 1 && <div className="-mt-1 mb-3 sm:hidden">{agentSelect("w-48")}</div>}

      {/* Always mounted, so a screen reader announces each change of its text. */}
      <p role="status" className="sr-only">
        {searchStatus}
      </p>
      {busy && (
        <LoadingSkeleton label="Searching transcripts">
          <Skeleton className="h-20 w-full" />
        </LoadingSkeleton>
      )}

      {!busy && searchError !== null && (
        <ListState kind="error" things="search results" error={searchError} onRetry={() => void run()} />
      )}

      {hits === null && searchError === null && (
        <SessionBriefsSection org={identity.org} namespace={identity.namespace} />
      )}
      {!busy && hits === null && searchError === null && threads === null && (
        <LoadingSkeleton label="Loading threads" className="space-y-2">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </LoadingSkeleton>
      )}

      {!busy && hits === null && searchError === null && threads !== null && threadsError !== null && (
        <ListState
          kind="error"
          things="conversations"
          error={threadsError}
          onRetry={() => {
            setThreads(null);
            setThreadsError(null);
            setThreadsRetry((n) => n + 1);
          }}
        />
      )}

      {!busy && hits === null && searchError === null && threads !== null && threads.length === 0 && !threadsError && (
        <ListState
          kind="empty"
          icon="chat"
          title="No conversations yet in this namespace"
          description="Once an AI agent connected to this namespace starts saving its chats, each thread shows up here."
          details={
            <>
              The agent calls <code className="font-mono text-xs">conversation.append</code> once per chat turn (the MCP
              tool description nudges it to).
            </>
          }
        />
      )}

      {!busy &&
        hits === null &&
        searchError === null &&
        threads !== null &&
        threads.length > 0 &&
        renderRecentThreads()}

      {!busy && hits !== null && hits.length === 0 && (
        <div className="space-y-6">
          <ListState
            kind="filtered"
            things="conversations"
            filter={`"${hitsFor}"`}
            onClear={() => {
              setQ("");
              setHits(null);
            }}
          />
          {renderRecentThreads("Or, the most recent threads")}
        </div>
      )}

      {/* One row per thread: its name, then up to three of its matching turns, each of which
          opens the replay at that turn. */}
      {!busy && groupedHits !== null && groupedHits.length > 0 && (
        <div ref={hitsNav} className="space-y-2">
          {groupedHits.map((g) => (
            <ListRow key={g.key} data-thread-row="">
              <button
                type="button"
                data-row-open=""
                onClick={() =>
                  openThreadDialog(g.thread_id, {
                    namespace: g.namespace,
                    knownAgent: g.agent,
                    knownTitle: g.title || UNTITLED_THREAD,
                  })
                }
                className="block w-full text-left bg-transparent border-0 p-0 cursor-pointer after:absolute after:inset-0 after:content-['']"
              >
                <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span
                    className={`min-w-0 flex-1 truncate text-sm font-medium ${g.title ? "text-text" : "text-text-muted"}`}
                  >
                    {g.title || UNTITLED_THREAD}
                  </span>
                  {g.namespace !== identity.namespace && (
                    <span className="min-w-0 truncate font-mono text-xs text-text-muted">{g.namespace}</span>
                  )}
                  <span className="shrink-0 text-xs text-text-muted tabular">
                    {g.hits.length} match{g.hits.length === 1 ? "" : "es"} · <ListTime iso={g.newest} quiet />
                  </span>
                </span>
              </button>
              <div className="mt-1 space-y-0.5">
                {g.hits.slice(0, HIT_LINES).map((h, i) => (
                  <button
                    type="button"
                    key={`${h.msg_id}-${i}`}
                    onClick={() =>
                      openThreadDialog(g.thread_id, {
                        namespace: g.namespace,
                        hit: h,
                        knownAgent: g.agent,
                        knownTitle: g.title || UNTITLED_THREAD,
                      })
                    }
                    className="relative z-[1] -mx-1 flex w-[calc(100%+0.5rem)] min-w-0 items-center gap-2 rounded px-1 py-0.5 text-left text-xs text-text-muted hover:bg-surface-2"
                  >
                    <span className="inline-flex shrink-0">
                      <Badge text={h.role} state="neutral" size="sm" />
                    </span>
                    {/* A space for the line's name ("user Are refunds…"); the flex gap draws it. */}{" "}
                    <span className="min-w-0 flex-1 truncate">
                      {emphasize(truncate(h.content_excerpt || "", 280), hitsFor)}
                    </span>
                  </button>
                ))}
              </div>
            </ListRow>
          ))}
        </div>
      )}

      <ThreadReplayDialog identity={identity} thread={openThread} onClose={closeThreadDialog} />
    </PageContainer>
  );
}
