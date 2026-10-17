import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Icon } from "@clickhouse/click-ui/Icon";
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
import { ListToolbar } from "../components/ui/ListToolbar";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../components/Toast";
import { displayAgent, relativeTime, shortDate, truncate, humanizeError, errorRequestId } from "../lib/format";
import { useAbortableEffect, useKeyboard } from "../lib/hooks";
import { IconButton } from "../components/IconButton";
import { SessionBriefsSection } from "../components/SessionBriefsSection";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";
import { ThreadReplayDialog, type ReplayThread } from "../components/ThreadReplayDialog";

const THREADS_PAGE = 30;

// The fields the Sessions list reads of a thread.
const THREADS_FIELDS = ["thread_id", "agent", "msg_count", "title", "last_role", "last_seen", "last_content_excerpt"];

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
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [threadsRetry, setThreadsRetry] = useState(0);
  // Threads come a page at a time, most recently active first, over the past year.
  const [threadPage, setThreadPage] = useState(0);
  const [threadsPaged, setThreadsPaged] = useState(true);
  // Known only once a page after the last full one comes back empty.
  const [threadsTotal, setThreadsTotal] = useState<number | undefined>(undefined);
  const [seenAgents, setSeenAgents] = useState<string[]>([]);
  const firstThreadRef = useRef<string | null>(null);
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
          if (threadPage === 0) firstThreadRef.current = rows[0]?.thread_id ?? null;
          else if (rows[0] && rows[0].thread_id === firstThreadRef.current) {
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
          const rid = errorRequestId(e);
          setThreadsError(rid ? `${humanizeError(e)} (request id ${rid})` : humanizeError(e));
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
    try {
      const r = await searchConversations(identity, q, { top_k: 50, agent: agentFilter });
      if (seq !== searchSeq.current) return;
      setHits(r.hits || []);
    } catch (e) {
      if (seq !== searchSeq.current) return;
      toast(`Search failed: ${humanizeError(e)}`, "error");
      setHits([]);
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
    const threadAgent = new Map((threads || []).map((t) => [t.thread_id, t.agent] as const));
    return Array.from(byThread.entries()).map(([key, items]) => {
      const { thread_id } = items[0];
      const namespace = items[0].namespace || identity.namespace;
      const agent = namespace === identity.namespace ? threadAgent.get(thread_id) : undefined;
      return { key, thread_id, namespace, agent, hits: items };
    });
  }, [hits, threads, identity.namespace]);

  const searchStatus = busy
    ? "Searching…"
    : groupedHits === null
      ? ""
      : groupedHits.length === 0
        ? "No matches"
        : `${hits?.length} match${hits?.length === 1 ? "" : "es"} in ${groupedHits.length} thread${groupedHits.length === 1 ? "" : "s"}`;

  // Distinct agents for the filter; the displayed thread list is the
  // namespace-wide set narrowed to the selected agent.
  // The agents seen on any page so far; the filter itself is the server's.
  const agentOptions = seenAgents;
  const visibleThreads = threads;

  // The open thread is the page's URL (`?thread=`): a reload or Back reopens it.
  const openThreadDialog = useCallback(
    (thread_id: string, opts: { namespace?: string; hit?: ConversationHit; knownAgent?: string } = {}) => {
      setViewParam("thread", thread_id);
      setOpenThread({
        thread_id,
        namespace: opts.namespace || identity.namespace,
        knownAgent: opts.knownAgent,
        hit: opts.hit,
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

  const threadsCount =
    hits !== null
      ? `${hits.length} found`
      : visibleThreads
        ? `${visibleThreads.length.toLocaleString()} threads`
        : undefined;

  const renderRecentThreads = (label = "Recent threads") => {
    if (!visibleThreads || visibleThreads.length === 0) return null;
    return (
      <div className="space-y-2">
        <div id="threads-list" tabIndex={-1} className="outline-none">
          <SectionHeading level={2} label={label} caption="active in the last year" />
        </div>
        {visibleThreads.map((t) => {
          // An untitled thread is named by its last message, so no row looks broken.
          const title = t.title || truncate(t.last_content_excerpt, 140);
          return (
            <ListRow key={t.thread_id} className="group">
              <button
                type="button"
                onClick={() => openThreadDialog(t.thread_id, { knownAgent: t.agent })}
                className="block w-full text-left bg-transparent border-0 p-0 cursor-pointer after:absolute after:inset-0 after:content-['']"
              >
                <span className="flex items-baseline gap-3">
                  <span className="min-w-0 flex-1 truncate text-sm font-medium text-text">{title}</span>
                  <span className="shrink-0 text-xs text-text-muted tabular">{shortDate(t.last_seen)}</span>
                </span>
                <span className="mt-1 flex items-center gap-2 text-xs text-text-muted">
                  <span className="inline-flex shrink-0">
                    <Badge text={t.last_role} state="neutral" size="sm" />
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    {t.title ? truncate(t.last_content_excerpt, 140) : null}
                  </span>
                  <span className="shrink-0 tabular">
                    {t.msg_count.toLocaleString()} message{t.msg_count === 1 ? "" : "s"} · {displayAgent(t.agent)}
                  </span>
                </span>
              </button>
            </ListRow>
          );
        })}
        {threadsPaged && hits === null && (
          <ListPager
            label="Thread pages"
            page={threadPage}
            pageSize={THREADS_PAGE}
            shown={visibleThreads.length}
            total={threadsTotal}
            onPage={setThreadPage}
            topId="threads-list"
          />
        )}
      </div>
    );
  };

  return (
    <PageContainer>
      <div className="mb-6">
        <PageHeader title="Conversations">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </PageHeader>
        <p className="text-sm text-text-muted">
          The raw chats your agents recorded. Search to find what was said; click any thread to replay the full
          exchange.
        </p>
      </div>

      {/* The search and the agent filter, in the bar that stays at the top as the threads
          scroll. Recording is a namespace setting, not a filter: it sits under the bar. */}
      <ListToolbar label="Search and filters" count={threadsCount}>
        <div className="min-w-[10rem] flex-1">
          <SearchField
            ref={searchRef}
            value={q}
            onChange={(value) => {
              setQ(value);
              if (!value) setHits(null);
            }}
            onKeyDown={(e) => e.key === "Enter" && run()}
            placeholder="Search transcripts"
            aria-label="Search conversation transcripts"
          />
        </div>
        {agentOptions.length > 1 && (
          <Select
            value={agentFilter}
            onSelect={(value) => {
              setAgentFilter(value);
              setThreadsTotal(undefined);
              setThreadPage(0);
            }}
            triggerProps={{ "aria-label": "Agent" }}
            className="hidden w-48 shrink-0 sm:block"
          >
            <Select.Item value="*">All agents</Select.Item>
            {agentOptions.map((a) => (
              <Select.Item key={a} value={a}>
                {displayAgent(a)}
              </Select.Item>
            ))}
          </Select>
        )}
      </ListToolbar>

      {(agentOptions.length > 1 || (canSetLogging && loggingEnabled !== null)) && (
        <div className="mb-4 flex items-center gap-3">
          {agentOptions.length > 1 && (
            // A phone's bar has room for the search only; the agent filter sits under it.
            <Select
              value={agentFilter}
              onSelect={(value) => {
                setAgentFilter(value);
                setThreadsTotal(undefined);
                setThreadPage(0);
              }}
              triggerProps={{ "aria-label": "Agent" }}
              className="w-48 sm:hidden"
            >
              <Select.Item value="*">All agents</Select.Item>
              {agentOptions.map((a) => (
                <Select.Item key={a} value={a}>
                  {displayAgent(a)}
                </Select.Item>
              ))}
            </Select>
          )}
          {canSetLogging && loggingEnabled !== null && (
            <div className="ml-auto flex items-center gap-1">
              <Switch
                checked={loggingEnabled}
                onCheckedChange={toggleLogging}
                label="Record conversations"
                orientation="horizontal"
                dir="start"
              />
              <IconButton
                icon="info-in-circle"
                size="xs"
                label="About recording"
                tooltip="When off, new conversation turns are not stored for this namespace"
              />
            </div>
          )}
        </div>
      )}

      {/* Always mounted, so a screen reader announces each change of its text. */}
      <p role="status" className="sr-only">
        {searchStatus}
      </p>
      {busy && (
        <LoadingSkeleton label="Searching transcripts">
          <Skeleton className="h-20 w-full" />
        </LoadingSkeleton>
      )}

      {hits === null && <SessionBriefsSection org={identity.org} namespace={identity.namespace} />}
      {!busy && hits === null && threads === null && (
        <LoadingSkeleton label="Loading threads" className="space-y-2">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </LoadingSkeleton>
      )}

      {!busy && hits === null && threads !== null && threads.length === 0 && threadsError && (
        <EmptyState
          icon="warning"
          title="Couldn't load conversations"
          description={threadsError}
          action={{
            label: "Retry",
            onClick: () => {
              setThreads(null);
              setThreadsRetry((n) => n + 1);
            },
          }}
        />
      )}

      {!busy && hits === null && threads !== null && threads.length === 0 && !threadsError && (
        <EmptyState
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

      {!busy && hits === null && threads !== null && threads.length > 0 && renderRecentThreads()}

      {!busy && hits !== null && hits.length === 0 && (
        <div className="space-y-6">
          <div className="py-8 text-center">
            <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-surface-2">
              <Icon name="search" size="md" aria-hidden="true" />
            </div>
            <h2>No matches for "{q}"</h2>
            <p className="mt-1 text-sm text-text-muted">Try a different phrase, or browse recent threads below.</p>
          </div>
          {renderRecentThreads("Or, the most recent threads")}
        </div>
      )}

      {!busy && groupedHits !== null && groupedHits.length > 0 && (
        <div className="space-y-3">
          <div className="text-xs text-text-muted tabular" aria-hidden="true">
            {searchStatus}
          </div>
          {groupedHits.map((g) => (
            <div key={g.key} className="rounded-xl border border-border bg-surface overflow-hidden">
              <button
                type="button"
                onClick={() => openThreadDialog(g.thread_id, { namespace: g.namespace, knownAgent: g.agent })}
                className="w-full text-left px-3.5 py-2.5 flex items-center gap-2 text-xs tabular hover:bg-surface-2 border-b border-border"
              >
                <span className="font-mono text-text truncate">{g.thread_id}</span>
                {g.agent && (
                  <>
                    <span className="text-text-muted">·</span>
                    <span className="font-mono text-text-muted">{displayAgent(g.agent)}</span>
                  </>
                )}
                {g.namespace !== identity.namespace && (
                  <>
                    <span className="text-text-muted">·</span>
                    <span className="font-mono text-text-muted truncate">{g.namespace}</span>
                  </>
                )}
                <span className="ml-auto text-accent font-medium">
                  {g.hits.length} match{g.hits.length === 1 ? "" : "es"} · open
                </span>
              </button>
              <div className="divide-y divide-border">
                {g.hits.map((h, i) => (
                  <button
                    type="button"
                    key={`${h.msg_id}-${i}`}
                    onClick={() =>
                      openThreadDialog(g.thread_id, { namespace: g.namespace, hit: h, knownAgent: g.agent })
                    }
                    className="w-full text-left px-3.5 py-2.5 hover:bg-surface-2"
                  >
                    <div className="flex items-center gap-2 mb-1 text-xs tabular">
                      <Badge text={h.role} state="neutral" size="sm" />
                      <span className="text-text-muted ml-auto">{relativeTime(h.ts)}</span>
                    </div>
                    <div className="text-sm text-text leading-snug">{truncate(h.content_excerpt || "", 280)}</div>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <ThreadReplayDialog identity={identity} thread={openThread} onClose={closeThreadDialog} />
    </PageContainer>
  );
}
