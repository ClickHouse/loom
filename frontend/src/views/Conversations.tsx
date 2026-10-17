import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
import { Switch } from "@clickhouse/click-ui/Switch";
import {
  searchConversations,
  listConversationThreads,
  getConversationThread,
  listConversationMessages,
  getNamespace,
  setConversationLogging,
  listMemories,
  type ConversationHit,
  type ConversationThreadSummary,
  type ConversationThreadDetail,
  type MemoryListEntry,
} from "../api";
import { useStore, useIdentity } from "../lib/state";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { Skeleton } from "../components/Skeleton";
import { Chip, ExternalLink, SectionHeading } from "../components/ui";
import { Modal } from "../components/ui/Modal";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../components/Toast";
import { displayAgent, relativeTime, truncate, humanizeError, errorRequestId } from "../lib/format";
import { useAbortableEffect, useKeyboard } from "../lib/hooks";
import { CopyButton } from "../components/CopyButton";
import { IconButton } from "../components/IconButton";
import { SessionBriefPanel } from "../components/SessionBriefPanel";
import { SessionBriefsSection } from "../components/SessionBriefsSection";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";

// The fields the Sessions list reads of a thread.
const THREADS_FIELDS = ["thread_id", "agent", "msg_count", "title", "last_role", "last_seen", "last_content_excerpt"];

// Role → visual treatment for the thread replay. Differentiating turns is the
// single biggest readability win: a coding chat is otherwise an undifferentiated
// wall of gray text with code indistinguishable from prose.
const ROLE_META: Record<string, { label: string; rail: string; card: string; muted?: boolean }> = {
  user: { label: "You", rail: "border-l-2 border-accent", card: "bg-surface" },
  assistant: {
    label: "Assistant",
    rail: "border-l-2 border-border",
    card: "bg-surface-2",
  },
  system: { label: "System", rail: "", card: "bg-transparent border border-dashed border-border", muted: true },
  tool: { label: "Tool", rail: "border-l-2 border-warn/60", card: "bg-surface-2" },
};

function linkify(text: string) {
  return text.split(/(https?:\/\/[^\s)]+)/g).map((p, i) =>
    /^https?:\/\//.test(p) ? (
      <ExternalLink key={i} href={p} className="break-all">
        {p}
      </ExternalLink>
    ) : (
      <span key={i}>{p}</span>
    ),
  );
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="my-2 rounded-lg border border-border bg-surface-2">
      <div className="flex items-center justify-between gap-2 px-3 pt-2">
        <span className="text-xs text-text-muted">{lang}</span>
        <CopyButton text={code} label="Copy" ariaLabel="Copy code" className="shrink-0" />
      </div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-words p-3 overflow-x-auto">{code}</pre>
    </div>
  );
}

// Render message content: split on ``` fences (code blocks), and within prose
// render inline `code` and linkify URLs. Zero deps — coding transcripts need
// code legible, not buried in a gray paragraph.
function MessageBody({ content, muted = false }: { content: string; muted?: boolean }) {
  const segs = content.split(/```/);
  return (
    <div className={`text-sm leading-relaxed ${muted ? "text-text-muted" : "text-text"}`}>
      {segs.map((seg, i) => {
        if (i % 2 === 1) {
          const nl = seg.indexOf("\n");
          const firstLine = nl >= 0 ? seg.slice(0, nl).trim() : "";
          const lang = /^[a-zA-Z0-9_+-]{1,20}$/.test(firstLine) ? firstLine : "";
          const code = (lang ? seg.slice(nl + 1) : seg).replace(/\n$/, "");
          return <CodeBlock key={i} code={code} lang={lang || undefined} />;
        }
        return (
          <span key={i} className="whitespace-pre-wrap break-words">
            {seg.split(/(`[^`]+`)/g).map((piece, j) =>
              piece.length > 1 && piece.startsWith("`") && piece.endsWith("`") ? (
                <code key={j} className="text-xs font-mono px-1 rounded bg-surface-2">
                  {piece.slice(1, -1)}
                </code>
              ) : (
                <span key={j}>{linkify(piece)}</span>
              ),
            )}
          </span>
        );
      })}
    </div>
  );
}

function ReasoningDisclosure({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-1">
      <Button
        type="empty"
        htmlType="button"
        iconLeft={open ? "chevron-down" : "chevron-right"}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        Thinking
      </Button>
      {open && (
        <div className="mt-1 text-xs text-text-muted italic whitespace-pre-wrap break-words border-l-2 border-border pl-2">
          {text}
        </div>
      )}
    </div>
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
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [threadsRetry, setThreadsRetry] = useState(0);
  // Selected thread for the message-replay dialog. Click a thread
  // card OR a search-hit thread header → fetch full messages → show.
  // `knownAgent`: the agent the opener named, for a replica of the previous release.
  const [openThread, setOpenThread] = useState<{
    thread_id: string;
    agent: string;
    namespace: string;
    knownAgent?: string;
  } | null>(null);
  const [threadDetail, setThreadDetail] = useState<ConversationThreadDetail | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  // Durable memories this thread produced (via source_trace=conversation:<id>).
  const [threadMemories, setThreadMemories] = useState<MemoryListEntry[]>([]);
  // When a thread is opened from a search hit, scroll to + highlight that message.
  const [highlightMsgId, setHighlightMsgId] = useState<string | null>(null);
  // Cursor for the next page of messages (conversation.list). Null
  // means "no more" — either we've walked to the end or the initial
  // fetch fit under the limit. The "Load more" button only renders
  // when this is set.
  const [moreCursor, setMoreCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const searchRef = useRef<HTMLInputElement | null>(null);
  // The open thread's reads (detail, memories, "Load more"); aborted when another thread
  // opens, the dialog closes or the view unmounts, so thread A's answers never land
  // under thread B.
  const threadReads = useRef<AbortController | null>(null);
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

  useEffect(() => () => threadReads.current?.abort(), []);

  // Hits, threads, the agent filter and the recording switch belong to the namespace
  // they were read for; a switch clears them so B never shows A's rows or toggles A's
  // value into B.
  useEffect(() => {
    searchSeq.current += 1;
    scopeGen.current += 1;
    setHits(null);
    setBusy(false);
    setThreads(null);
    setAgentFilter("*");
    setLoggingEnabled(null);
  }, [identity.org, identity.namespace]);

  useAbortableEffect(
    (signal, live) => {
      setThreadsError(null);
      listConversationThreads(
        { org: identity.org, namespace: identity.namespace },
        { limit: 50, signal, fields: THREADS_FIELDS },
      )
        .then((r) => {
          if (live()) setThreads(r.threads || []);
        })
        .catch((e) => {
          if (!live()) return;
          // An empty list that is really a failed read looked like "no
          // conversations yet".
          const rid = errorRequestId(e);
          setThreadsError(rid ? `${humanizeError(e)} (request id ${rid})` : humanizeError(e));
          setThreads([]);
        });
      getNamespace(identity, { signal })
        .then((n) => {
          if (live()) setLoggingEnabled(n?.conversation_logging_enabled ?? true);
        })
        .catch(() => {
          if (live()) setLoggingEnabled(null);
        });
    },
    [identity.org, identity.namespace, threadsRetry],
  );

  // Scroll to the message a search hit pointed at, once the thread loads.
  useEffect(() => {
    if (!highlightMsgId || !threadDetail) return;
    const t = setTimeout(
      () => document.getElementById(`msg-${highlightMsgId}`)?.scrollIntoView({ block: "center", behavior: "smooth" }),
      60,
    );
    return () => clearTimeout(t);
  }, [highlightMsgId, threadDetail]);

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

  // Distinct agents for the filter; the displayed thread list is the
  // namespace-wide set narrowed to the selected agent.
  const agentOptions = useMemo(() => Array.from(new Set((threads || []).map((t) => t.agent))).sort(), [threads]);
  const visibleThreads = useMemo(
    () => (agentFilter === "*" ? threads : (threads || []).filter((t) => t.agent === agentFilter)),
    [threads, agentFilter],
  );

  const openThreadDialog = useCallback(
    async (thread_id: string, opts: { namespace?: string; msgId?: string; knownAgent?: string } = {}) => {
      threadReads.current?.abort();
      const ctl = new AbortController();
      threadReads.current = ctl;
      const { signal } = ctl;
      const namespace = opts.namespace || identity.namespace;
      const targetMsgId = opts.msgId;
      // "*" reads every agent's turns in the thread, however it was opened.
      setOpenThread({ thread_id, agent: "*", namespace, knownAgent: opts.knownAgent });
      setThreadDetail(null);
      setMoreCursor(null);
      setLoadingMore(false);
      setThreadMemories([]);
      setHighlightMsgId(targetMsgId ?? null);
      setThreadLoading(true);
      // What durable memories did this conversation produce? (provenance link)
      listMemories({ ...identity, namespace }, 20, {
        source_trace: `conversation:${thread_id}`,
        include_profile: true,
        signal,
      })
        .then((r) => {
          if (!signal.aborted) setThreadMemories(r.entries);
        })
        .catch(() => {
          if (!signal.aborted) setThreadMemories([]);
        });
      try {
        const read = (agent: string) =>
          getConversationThread({ org: identity.org, namespace }, agent, thread_id, { limit: 200, signal });
        let d = await read("*");
        // A replica of the previous release reads "*" as a literal agent; the thread
        // list's agent still opens the thread there. Drop with the next release.
        if (!d.found && opts.knownAgent && !signal.aborted) {
          const agent = opts.knownAgent;
          d = await read(agent);
          if (signal.aborted) return;
          if (d.found) setOpenThread((t) => t && { ...t, agent });
        }
        if (signal.aborted) return;
        setThreadDetail(d);
        // More turns follow: conversation.list pages on from the cursor get issued.
        setMoreCursor(d.next_cursor ?? null);
      } catch (e) {
        if (signal.aborted) return;
        toast(`Open thread failed: ${humanizeError(e)}`, "error");
      } finally {
        if (!signal.aborted) setThreadLoading(false);
      }
    },
    [identity],
  );

  const closeThreadDialog = () => {
    threadReads.current?.abort();
    threadReads.current = null;
    setOpenThread(null);
    setThreadDetail(null);
  };

  // Deep-link from a memory's "View source conversation" → open that thread.
  useEffect(() => {
    if (!pendingConversationThread) return;
    const { threadId, agent } = pendingConversationThread;
    void openThreadDialog(threadId, { knownAgent: agent || undefined });
    clearPendingConversationThread();
  }, [pendingConversationThread, openThreadDialog, clearPendingConversationThread]);

  const loadMoreMessages = async () => {
    const reads = threadReads.current;
    if (!reads || !openThread || !threadDetail || !moreCursor || loadingMore) return;
    const { signal } = reads;
    const { thread_id, agent, namespace, knownAgent } = openThread;
    setLoadingMore(true);
    try {
      const page = (as: string) =>
        listConversationMessages({ org: identity.org, namespace }, as, thread_id, {
          cursor: moreCursor,
          limit: 200,
          signal,
        });
      let r = await page(agent);
      // A replica of the previous release answers "*" with no rows; the opener's agent
      // still pages there. Drop with the next release.
      if (r.rows.length === 0 && agent === "*" && knownAgent && !signal.aborted) {
        r = await page(knownAgent);
        if (signal.aborted) return;
        setOpenThread((t) => t && { ...t, agent: knownAgent });
      }
      if (signal.aborted) return;
      // Merge into the detail as it is now, and only if it is still this thread's. New
      // rows append (server returns chronological order; we render in-place).
      setThreadDetail((prev) => {
        if (!prev || prev.thread_id !== thread_id) return prev;
        return { ...prev, messages: [...prev.messages, ...r.rows] };
      });
      setMoreCursor(r.next_cursor);
    } catch (e) {
      if (signal.aborted) return;
      toast(`Load more failed: ${humanizeError(e)}`, "error");
    } finally {
      if (!signal.aborted) setLoadingMore(false);
    }
  };

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

  const renderRecentThreads = (label = "Recent threads") => {
    if (!visibleThreads || visibleThreads.length === 0) return null;
    return (
      <div className="space-y-2">
        <SectionHeading label={label} count={visibleThreads.length} />
        {visibleThreads.map((t) => (
          <button
            type="button"
            key={t.thread_id}
            onClick={() => openThreadDialog(t.thread_id, { knownAgent: t.agent })}
            className="w-full text-left p-3 rounded-xl border border-border bg-surface hover:border-border-strong flex items-start gap-3"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-1 text-xs tabular">
                <span className="font-mono text-xs text-text-muted">{displayAgent(t.agent)}</span>
                <span className="text-text-muted">·</span>
                <span className="text-text-muted">
                  {t.msg_count} msg{t.msg_count === 1 ? "" : "s"}
                </span>
              </div>
              {t.title ? <div className="text-sm font-medium text-text truncate">{t.title}</div> : null}
              <div className={`text-sm truncate ${t.title ? "text-text-muted" : "text-text"}`}>
                <span className="text-xs font-medium text-text-muted mr-1.5">{t.last_role}</span>
                {truncate(t.last_content_excerpt, 140)}
              </div>
            </div>
            <span className="text-xs tabular text-text-muted shrink-0 mt-0.5">{relativeTime(t.last_seen)}</span>
          </button>
        ))}
      </div>
    );
  };

  return (
    <PageContainer>
      <div className="mb-6">
        <h1 className="text-2xl mb-1">Conversations</h1>
        <div className="mb-2">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <p className="text-sm text-text-muted">
          The raw chats your agents recorded. Search to find what was said; click any thread to replay the full
          exchange.
        </p>
      </div>

      <div className="mb-6">
        <SearchField
          ref={searchRef}
          value={q}
          onChange={(value) => {
            setQ(value);
            if (!value) setHits(null);
          }}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="Search transcripts (press / to focus)…"
          aria-label="Search conversation transcripts"
        />
      </div>

      {(agentOptions.length > 1 || (canSetLogging && loggingEnabled !== null)) && (
        <div className="flex items-center gap-3 mb-5 -mt-3">
          {agentOptions.length > 1 && (
            <Select
              label="Agent"
              orientation="horizontal"
              value={agentFilter}
              onSelect={(value) => setAgentFilter(value)}
              className="w-64"
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

      {busy && <Skeleton className="h-20 w-full" />}

      {hits === null && <SessionBriefsSection org={identity.org} namespace={identity.namespace} />}
      {!busy && hits === null && threads === null && (
        <div className="space-y-2">
          <Skeleton className="h-14" />
          <Skeleton className="h-14" />
        </div>
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
          <div className="text-xs text-text-muted tabular">
            {hits?.length} match{hits?.length === 1 ? "" : "es"} in {groupedHits.length} thread
            {groupedHits.length === 1 ? "" : "s"}
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
                      openThreadDialog(g.thread_id, { namespace: g.namespace, msgId: h.msg_id, knownAgent: g.agent })
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

      {/* Thread replay dialog. conversation.get(include_messages=true)
          returns the first 200 messages; we render them as a chat
          transcript with role chips, and "Load more" walks the rest
          via conversation.list. */}
      <Modal
        open={openThread !== null}
        onOpenChange={(v) => {
          if (!v) closeThreadDialog();
        }}
        title={threadDetail?.title || "Thread"}
        className="max-w-[820px] w-full"
        initialFocus="dialog"
      >
        <div className="text-xs text-text-muted font-mono truncate">{openThread?.thread_id}</div>
        {openThread && openThread.namespace !== identity.namespace && (
          <div className="mt-1">
            <ScopeBadge kind="namespace" value={openThread.namespace} />
          </div>
        )}
        {threadDetail && (
          <div className="text-xs text-text-muted mt-1 flex items-center gap-2 flex-wrap">
            <span>
              {threadDetail.msg_count} message{threadDetail.msg_count === 1 ? "" : "s"}
            </span>
            {threadDetail.agent && (
              <>
                <span>·</span>
                <span className="font-mono">{displayAgent(threadDetail.agent)}</span>
              </>
            )}
            {threadDetail.updated_at && (
              <>
                <span>·</span>
                <span>updated {relativeTime(threadDetail.updated_at)}</span>
              </>
            )}
          </div>
        )}
        <div className="mt-4">
          {openThread && (
            <SessionBriefPanel org={identity.org} namespace={openThread.namespace} sessionId={openThread.thread_id} />
          )}
          {threadMemories.length > 0 && (
            <div className="mb-4 p-3 rounded-lg bg-surface-2 border border-border">
              <SectionHeading label="Memories this thread produced" count={threadMemories.length} />
              <div className="flex flex-wrap gap-1.5">
                {threadMemories.map((m) => (
                  <Chip
                    key={m.memory_key}
                    label={m.memory_key}
                    mono
                    tip={typeof m.value === "string" ? m.value : JSON.stringify(m.value ?? "")}
                  />
                ))}
              </div>
            </div>
          )}
          {threadLoading && <div className="text-center text-sm text-text-muted py-12">Loading…</div>}
          {!threadLoading && threadDetail && !threadDetail.found && (
            <div className="text-center text-sm text-text-muted py-12">
              {/* "or upgraded" goes with the previous-release fallback above. */}
              Thread not found. It may have been deleted, or a server is being upgraded: try again shortly.
            </div>
          )}
          {!threadLoading && threadDetail && threadDetail.found && threadDetail.messages.length === 0 && (
            <div className="text-center text-sm text-text-muted py-12">Thread exists but contains no messages.</div>
          )}
          {!threadLoading && threadDetail && threadDetail.messages.length > 0 && (
            <div className="space-y-2.5">
              {threadDetail.messages.map((m) => {
                const meta = ROLE_META[m.role] || ROLE_META.assistant;
                const label =
                  m.role === "assistant" ? displayAgent(m.agent || threadDetail.agent || "assistant") : meta.label;
                const highlighted = m.msg_id === highlightMsgId;
                return (
                  <div
                    key={m.msg_id}
                    id={`msg-${m.msg_id}`}
                    className={`rounded-lg px-3 py-2 ${meta.rail} ${meta.card} ${highlighted ? "ring-2 ring-accent" : ""}`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-xs font-medium text-text-muted">{label}</span>
                      {typeof m.tokens === "number" && m.tokens > 0 && (
                        <span className="text-xs text-text-muted">· {m.tokens} tok</span>
                      )}
                      <time dateTime={m.ts} className="text-xs text-text-muted ml-auto tabular">
                        {relativeTime(m.ts)}
                        <span className="sr-only"> ({m.ts})</span>
                      </time>
                    </div>
                    {m.reasoning ? <ReasoningDisclosure text={m.reasoning} /> : null}
                    <MessageBody content={m.content} muted={meta.muted} />
                  </div>
                );
              })}
              {moreCursor && (
                <div className="pt-3 flex justify-center">
                  <Button type="secondary" htmlType="button" loading={loadingMore} onClick={loadMoreMessages}>
                    {`Load more (${threadDetail.msg_count - threadDetail.messages.length} remaining)`}
                  </Button>
                </div>
              )}
              {/* No cursor though turns remain: a replica of the previous release read it. */}
              {!moreCursor && threadDetail.msg_count > threadDetail.messages.length && (
                <p className="pt-3 text-center text-xs text-text-muted">
                  {`Showing ${threadDetail.messages.length} of ${threadDetail.msg_count} turns.`}
                </p>
              )}
            </div>
          )}
        </div>
      </Modal>
    </PageContainer>
  );
}
