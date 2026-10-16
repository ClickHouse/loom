import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { MessagesSquare, Search, X as XIcon, AlertTriangle } from "lucide-react";
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
import { Chip, SectionHeading } from "../components/ui";
import { EmptyState } from "../components/EmptyState";
import { toast } from "../components/Toast";
import { displayAgent, relativeTime, truncate, humanizeError, errorRequestId } from "../lib/format";
import { useAbortableEffect, useKeyboard } from "../lib/hooks";
import { CopyButton } from "../components/CopyButton";
import { SessionBriefPanel } from "../components/SessionBriefPanel";
import { SessionBriefsSection } from "../components/SessionBriefsSection";
import { PageContainer } from "../layout/PageContainer";
import { ScopeBadge } from "../components/ScopeBadge";

// The fields the Sessions list reads of a thread.
const THREADS_FIELDS = ["thread_id", "agent", "msg_count", "title", "last_role", "last_seen", "last_content_excerpt"];

// Role → visual treatment for the thread replay. Differentiating turns is the
// single biggest readability win: a coding chat is otherwise an undifferentiated
// wall of gray text with code indistinguishable from prose.
const ROLE_META: Record<string, { label: string; rail: string; card: string }> = {
  user: { label: "You", rail: "border-l-2 border-[var(--color-accent)]", card: "bg-[var(--color-surface)]" },
  assistant: {
    label: "Assistant",
    rail: "border-l-2 border-[var(--color-border)]",
    card: "bg-[var(--color-surface-2)]",
  },
  system: { label: "System", rail: "", card: "bg-transparent opacity-70" },
  tool: { label: "Tool", rail: "border-l-2 border-warn/60", card: "bg-[var(--color-surface-2)]" },
};

function linkify(text: string) {
  return text.split(/(https?:\/\/[^\s)]+)/g).map((p, i) =>
    /^https?:\/\//.test(p) ? (
      <a key={i} href={p} target="_blank" rel="noreferrer" className="text-[var(--color-accent)] underline break-all">
        {p}
      </a>
    ) : (
      <span key={i}>{p}</span>
    ),
  );
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="relative group my-2">
      {lang && (
        <span className="absolute left-2 top-1 text-2xs uppercase tracking-wide text-[var(--color-text-subtle)]">
          {lang}
        </span>
      )}
      <CopyButton
        text={code}
        label="Copy"
        ariaLabel="Copy code"
        className="absolute right-2 top-1 inline-flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded bg-[var(--color-surface)] border border-[var(--color-border)] opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity"
      />
      <pre className="text-xs font-mono whitespace-pre-wrap break-words bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded-lg p-3 pt-5 overflow-x-auto">
        {code}
      </pre>
    </div>
  );
}

// Render message content: split on ``` fences (code blocks), and within prose
// render inline `code` and linkify URLs. Zero deps — coding transcripts need
// code legible, not buried in a gray paragraph.
function MessageBody({ content }: { content: string }) {
  const segs = content.split(/```/);
  return (
    <div className="text-sm text-[var(--color-text)] leading-relaxed">
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
                <code key={j} className="text-xs font-mono px-1 rounded bg-[var(--color-surface-2)]">
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
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-2xs text-[var(--color-text-subtle)] hover:text-[var(--color-text-muted)]"
      >
        {open ? "▾ Thinking" : "▸ Thinking"}
      </button>
      {open && (
        <div className="mt-1 text-xs text-[var(--color-text-muted)] italic whitespace-pre-wrap break-words border-l-2 border-[var(--color-border)] pl-2">
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
  const [openThread, setOpenThread] = useState<{ thread_id: string; agent: string } | null>(null);
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

  // Group search hits by thread_id so multiple matches in the same
  // conversation collapse to one card with a hit count, not N
  // duplicates. Within a thread, hits stay in server order (relevance).
  // The thread's agent is taken from the first hit (server returns
  // one agent per thread).
  const groupedHits = useMemo(() => {
    if (!hits) return null;
    const byThread = new Map<string, ConversationHit[]>();
    for (const h of hits) {
      const arr = byThread.get(h.thread_id) || [];
      arr.push(h);
      byThread.set(h.thread_id, arr);
    }
    // threadsMap helps annotate each group with the agent — we need
    // an agent to call conversation.get. Search hits don't carry the
    // agent today, so resolve via the recent-threads list when
    // possible; fall back to "*" (the server accepts agent=* on .get).
    const threadAgent = new Map((threads || []).map((t) => [t.thread_id, t.agent] as const));
    return Array.from(byThread.entries()).map(([thread_id, items]) => ({
      thread_id,
      agent: threadAgent.get(thread_id) || "*",
      hits: items,
    }));
  }, [hits, threads]);

  // Distinct agents for the filter; the displayed thread list is the
  // namespace-wide set narrowed to the selected agent.
  const agentOptions = useMemo(() => Array.from(new Set((threads || []).map((t) => t.agent))).sort(), [threads]);
  const visibleThreads = useMemo(
    () => (agentFilter === "*" ? threads : (threads || []).filter((t) => t.agent === agentFilter)),
    [threads, agentFilter],
  );

  const openThreadDialog = useCallback(
    async (thread_id: string, agent: string, targetMsgId?: string) => {
      threadReads.current?.abort();
      const ctl = new AbortController();
      threadReads.current = ctl;
      const { signal } = ctl;
      setOpenThread({ thread_id, agent });
      setThreadDetail(null);
      setMoreCursor(null);
      setLoadingMore(false);
      setThreadMemories([]);
      setHighlightMsgId(targetMsgId ?? null);
      setThreadLoading(true);
      // What durable memories did this conversation produce? (provenance link)
      listMemories(identity, 20, { source_trace: `conversation:${thread_id}`, include_profile: true, signal })
        .then((r) => {
          if (!signal.aborted) setThreadMemories(r.entries);
        })
        .catch(() => {
          if (!signal.aborted) setThreadMemories([]);
        });
      try {
        const d = await getConversationThread({ org: identity.org, namespace: identity.namespace }, agent, thread_id, {
          limit: 200,
          signal,
        });
        if (signal.aborted) return;
        setThreadDetail(d);
        // If the thread's reported msg_count exceeds what we loaded,
        // there are more pages to walk via conversation.list. Cursor
        // value is the last message's id — the server resolves it to
        // a (ts, msg_id) seek key on conversations.
        if (d.messages.length > 0 && d.msg_count > d.messages.length) {
          const lastMsg = d.messages[d.messages.length - 1];
          setMoreCursor(lastMsg.msg_id);
        }
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
    void openThreadDialog(pendingConversationThread, "*");
    clearPendingConversationThread();
  }, [pendingConversationThread, openThreadDialog, clearPendingConversationThread]);

  const loadMoreMessages = async () => {
    const reads = threadReads.current;
    if (!reads || !openThread || !threadDetail || !moreCursor || loadingMore) return;
    const { signal } = reads;
    const { thread_id, agent } = openThread;
    setLoadingMore(true);
    try {
      const r = await listConversationMessages({ org: identity.org, namespace: identity.namespace }, agent, thread_id, {
        cursor: moreCursor,
        limit: 200,
        signal,
      });
      if (signal.aborted) return;
      // Merge into the detail as it is now, and only if it is still this thread's:
      // dedupe on msg_id in case the cursor boundary returns an overlap row. New rows
      // append (server returns chronological order; we render in-place).
      setThreadDetail((prev) => {
        if (!prev || prev.thread_id !== thread_id) return prev;
        const seen = new Set(prev.messages.map((m) => m.msg_id));
        const fresh = r.rows.filter((m) => !seen.has(m.msg_id));
        return { ...prev, messages: [...prev.messages, ...fresh] };
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
            key={t.thread_id}
            onClick={() => openThreadDialog(t.thread_id, t.agent)}
            className="w-full text-left p-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] hover:border-[var(--color-border-strong)] flex items-start gap-3"
            title="Open this thread"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-1 text-2xs tabular">
                <span className="font-mono text-2xs text-[var(--color-text-muted)]">{displayAgent(t.agent)}</span>
                <span className="text-[var(--color-text-muted)]">·</span>
                <span className="text-[var(--color-text-muted)]">
                  {t.msg_count} msg{t.msg_count === 1 ? "" : "s"}
                </span>
              </div>
              {t.title ? (
                <div className="text-base font-medium text-[var(--color-text)] truncate">{t.title}</div>
              ) : null}
              <div
                className={`text-sm truncate ${t.title ? "text-[var(--color-text-muted)]" : "text-[var(--color-text)]"}`}
              >
                <span className="uppercase text-2xs font-semibold text-[var(--color-text-subtle)] mr-1.5">
                  {t.last_role}
                </span>
                {truncate(t.last_content_excerpt, 140)}
              </div>
            </div>
            <span className="text-2xs tabular text-[var(--color-text-muted)] shrink-0 mt-0.5">
              {relativeTime(t.last_seen)}
            </span>
          </button>
        ))}
      </div>
    );
  };

  return (
    <PageContainer>
      <div className="mb-6">
        <h1 className="text-3xl font-semibold tracking-tight mb-1">Conversations</h1>
        <div className="mb-2">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <p className="text-base text-[var(--color-text-muted)]">
          The raw chats your agents recorded. Search to find what was said; click any thread to replay the full
          exchange.
        </p>
      </div>

      <div className="relative mb-6">
        <Search size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-[var(--color-text-muted)]" />
        <input
          ref={searchRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="Search transcripts (press / to focus)…"
          aria-label="Search conversation transcripts"
          className="w-full pl-10 pr-9 py-2.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] text-md outline-none focus:border-[var(--color-accent)]"
        />
        {q && (
          <button
            onClick={() => {
              setQ("");
              setHits(null);
            }}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-[var(--color-text-muted)] hover:text-[var(--color-text)] rounded"
            aria-label="Clear search"
          >
            <XIcon size={14} />
          </button>
        )}
      </div>

      {(agentOptions.length > 1 || (canSetLogging && loggingEnabled !== null)) && (
        <div className="flex items-center gap-3 mb-5 -mt-3">
          {agentOptions.length > 1 && (
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-text-muted)]">
              Agent
              <select
                value={agentFilter}
                onChange={(e) => setAgentFilter(e.target.value)}
                className="text-xs rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 outline-none focus:border-[var(--color-accent)]"
              >
                <option value="*">All agents</option>
                {agentOptions.map((a) => (
                  <option key={a} value={a}>
                    {displayAgent(a)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {canSetLogging && loggingEnabled !== null && (
            <button
              onClick={toggleLogging}
              className="ml-auto flex items-center gap-2 text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
              title="When off, new conversation turns are not stored for this namespace"
            >
              <span
                className={`inline-flex items-center w-7 h-4 rounded-full transition-colors ${loggingEnabled ? "bg-[var(--color-accent)]" : "bg-[var(--color-surface-2)] border border-[var(--color-border)]"}`}
              >
                <span
                  className={`block w-3 h-3 rounded-full bg-text shadow transition-transform ${loggingEnabled ? "translate-x-3.5" : "translate-x-0.5"}`}
                />
              </span>
              Recording {loggingEnabled ? "on" : "off"}
            </button>
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
          icon={AlertTriangle}
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
          icon={MessagesSquare}
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
            <div className="mx-auto mb-3 grid place-items-center w-12 h-12 rounded-2xl bg-[var(--color-surface-2)] text-[var(--color-text-muted)]">
              <Search size={20} strokeWidth={1.5} />
            </div>
            <h2 className="text-lg font-semibold tracking-tight">No matches for "{q}"</h2>
            <p className="mt-1 text-sm text-[var(--color-text-muted)]">
              Try a different phrase, or browse recent threads below.
            </p>
          </div>
          {renderRecentThreads("Or, the most recent threads")}
        </div>
      )}

      {!busy && groupedHits !== null && groupedHits.length > 0 && (
        <div className="space-y-3">
          <div className="text-xs text-[var(--color-text-muted)] tabular">
            {hits?.length} match{hits?.length === 1 ? "" : "es"} in {groupedHits.length} thread
            {groupedHits.length === 1 ? "" : "s"}
          </div>
          {groupedHits.map((g) => (
            <div
              key={g.thread_id}
              className="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] overflow-hidden"
            >
              <button
                onClick={() => openThreadDialog(g.thread_id, g.agent)}
                className="w-full text-left px-3.5 py-2.5 flex items-center gap-2 text-2xs tabular hover:bg-[var(--color-surface-2)] border-b border-[var(--color-border)]"
                title="Open this thread"
              >
                <span className="font-mono text-[var(--color-text)] truncate">{g.thread_id}</span>
                <span className="text-[var(--color-text-muted)]">·</span>
                <span className="font-mono text-[var(--color-text-muted)]">{displayAgent(g.agent)}</span>
                <span className="ml-auto text-[var(--color-accent)] font-medium">
                  {g.hits.length} match{g.hits.length === 1 ? "" : "es"} · open ↗
                </span>
              </button>
              <div className="divide-y divide-[var(--color-border)]">
                {g.hits.map((h, i) => (
                  <button
                    key={`${h.msg_id}-${i}`}
                    onClick={() => openThreadDialog(g.thread_id, g.agent, h.msg_id)}
                    className="w-full text-left px-3.5 py-2.5 hover:bg-[var(--color-surface-2)]"
                    title="Open thread at this message"
                  >
                    <div className="flex items-center gap-2 mb-1 text-2xs tabular">
                      <span className="px-1.5 py-0.5 rounded bg-[var(--color-surface-2)] uppercase font-semibold text-[var(--color-text-muted)]">
                        {h.role}
                      </span>
                      <span className="text-[var(--color-text-subtle)] ml-auto">{relativeTime(h.ts)}</span>
                    </div>
                    <div className="text-base text-[var(--color-text)] leading-snug">
                      {truncate(h.content_excerpt || "", 280)}
                    </div>
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
      <Dialog.Root
        open={openThread !== null}
        onOpenChange={(v) => {
          if (!v) closeThreadDialog();
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-overlay animate-fade-in motion-reduce:animate-none!" />
          <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[min(820px,94vw)] h-[min(85vh,800px)] flex flex-col rounded-2xl bg-[var(--color-surface)] border border-[var(--color-border)] shadow-2xl animate-fade-in motion-reduce:animate-none!">
            <div className="flex items-start justify-between gap-4 px-6 py-4 border-b border-[var(--color-border)] shrink-0">
              <div className="min-w-0 flex-1">
                <Dialog.Title className="text-md font-semibold tracking-tight truncate">
                  {threadDetail?.title || "Thread"}
                </Dialog.Title>
                <div className="text-2xs text-[var(--color-text-muted)] mt-1 font-mono truncate">
                  {openThread?.thread_id}
                </div>
                {threadDetail && (
                  <div className="text-2xs text-[var(--color-text-muted)] mt-1 flex items-center gap-2 flex-wrap">
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
              </div>
              <Dialog.Close
                className="p-1.5 text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
                aria-label="Close"
              >
                <XIcon size={16} />
              </Dialog.Close>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-4">
              {openThread && (
                <SessionBriefPanel org={identity.org} namespace={identity.namespace} sessionId={openThread.thread_id} />
              )}
              {threadMemories.length > 0 && (
                <div className="mb-4 p-3 rounded-lg bg-[var(--color-surface-2)] border border-[var(--color-border)]">
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
              {threadLoading && (
                <div className="text-center text-sm text-[var(--color-text-muted)] py-12">Loading…</div>
              )}
              {!threadLoading && threadDetail && !threadDetail.found && (
                <div className="text-center text-sm text-[var(--color-text-muted)] py-12">
                  Thread not found — it may have been deleted.
                </div>
              )}
              {!threadLoading && threadDetail && threadDetail.found && threadDetail.messages.length === 0 && (
                <div className="text-center text-sm text-[var(--color-text-muted)] py-12">
                  Thread exists but contains no messages.
                </div>
              )}
              {!threadLoading && threadDetail && threadDetail.messages.length > 0 && (
                <div className="space-y-2.5">
                  {threadDetail.messages.map((m) => {
                    const meta = ROLE_META[m.role] || ROLE_META.assistant;
                    const label = m.role === "assistant" ? displayAgent(threadDetail.agent || "assistant") : meta.label;
                    const highlighted = m.msg_id === highlightMsgId;
                    return (
                      <div
                        key={m.msg_id}
                        id={`msg-${m.msg_id}`}
                        className={`rounded-lg px-3 py-2 ${meta.rail} ${meta.card} ${highlighted ? "ring-2 ring-[var(--color-accent)]" : ""}`}
                      >
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-2xs uppercase font-semibold tracking-wide text-[var(--color-text-muted)]">
                            {label}
                          </span>
                          {typeof m.tokens === "number" && m.tokens > 0 && (
                            <span className="text-2xs text-[var(--color-text-subtle)]">· {m.tokens} tok</span>
                          )}
                          <span className="text-2xs text-[var(--color-text-subtle)] ml-auto tabular" title={m.ts}>
                            {relativeTime(m.ts)}
                          </span>
                        </div>
                        {m.reasoning ? <ReasoningDisclosure text={m.reasoning} /> : null}
                        <MessageBody content={m.content} />
                      </div>
                    );
                  })}
                  {moreCursor && (
                    <div className="pt-3 flex justify-center">
                      <button
                        onClick={loadMoreMessages}
                        disabled={loadingMore}
                        className="px-3 py-1.5 text-sm font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] disabled:opacity-50"
                      >
                        {loadingMore
                          ? "Loading…"
                          : `Load more (${threadDetail.msg_count - threadDetail.messages.length} remaining)`}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </PageContainer>
  );
}
