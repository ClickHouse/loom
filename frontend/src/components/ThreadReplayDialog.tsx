import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Badge } from "@clickhouse/click-ui/Badge";
import { Button } from "@clickhouse/click-ui/Button";
import {
  getConversationThread,
  listConversationMessages,
  listMemories,
  type ConversationHit,
  type ConversationMessage,
  type ConversationThreadDetail,
  type Identity,
  type MemoryListEntry,
} from "../api";
import { Chip, ExternalLink, ListTime, Loading, SectionHeading } from "./ui";
import { Modal } from "./ui/Modal";
import { BusyButton } from "./ui/BusyButton";
import { ListState } from "./ui/ListState";
import { toastError } from "./Toast";
import { displayAgent } from "../lib/format";
import { AgentBadge } from "./AgentBadge";
import { CopyButton } from "./CopyButton";
import { SessionBriefPanel } from "./SessionBriefPanel";
import { ScopeBadge } from "./ScopeBadge";

/** The thread a replay shows: its id and namespace, and the search hit it was opened
 *  from, if any. */
export interface ReplayThread {
  thread_id: string;
  namespace: string;
  hit?: ConversationHit;
  /** The name its opener shows (`threadTitle`), until the thread's own detail lands. */
  knownTitle?: string;
}

export const UNTITLED_THREAD = "Untitled thread";

/** What a thread is called: its title, else the first thing the user said
 *  in it, flattened to one line; "" when neither is known, which a caller shows as
 *  "Untitled thread". Never the thread id. */
export function threadTitle(title: string | null | undefined, firstUserMessage?: string | null): string {
  return (title || "").trim() || (firstUserMessage || "").replace(/\s+/g, " ").trim();
}

// Role → treatment for the thread replay. The role is a neutral badge on each turn, and the
// card's fill tells prose turns from the dashed system ones: colour stays for meaning.
const ROLE_META: Record<string, { label: string; card: string; muted?: boolean }> = {
  user: { label: "You", card: "border-l-2 border-border bg-surface" },
  assistant: { label: "Assistant", card: "border-l-2 border-border bg-surface-2" },
  system: { label: "System", card: "bg-transparent border border-dashed border-border", muted: true },
  tool: { label: "Tool", card: "border-l-2 border-border bg-surface-2" },
};

// Click UI's Badge measures its text on every render; a long transcript re-renders each
// turn on every "Load more", so the badge renders again only when its label changes.
const RoleBadge = memo(function RoleBadge({ label }: { label: string }) {
  return <Badge text={label} state="neutral" size="sm" />;
});

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

// A turn's element id: a msg_id is unique per agent only, and a thread read across
// agents can hold two agents' turns under one id.
function turnId(m: ConversationMessage): string {
  return `msg-${m.agent}-${m.msg_id}`;
}

// A fenced block in a transcript turn: the text as recorded, with a Copy button. Not Click
// UI's CodeBlock, which highlights by language and would guess at agents' mixed output.
function TranscriptCode({ code, lang }: { code: string; lang?: string }) {
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
          return <TranscriptCode key={i} code={code} lang={lang || undefined} />;
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

/**
 * The thread replay: conversation.get(include_messages=true) returns the first 200
 * messages, rendered as a chat transcript with role chips, and "Load more" walks the rest
 * via conversation.list. Conversations opens it on a thread row or a search hit; the
 * memory drawer opens it over itself on "View source conversation" (with `actions`, an
 * "Open in Conversations" link). A new `thread` object reads the thread again; null closes.
 */
export function ThreadReplayDialog({
  identity,
  thread,
  onClose,
  actions,
}: {
  identity: Identity;
  thread: ReplayThread | null;
  onClose: () => void;
  /** Under the thread's header: what else the opener offers. */
  actions?: ReactNode;
}) {
  const [threadDetail, setThreadDetail] = useState<ConversationThreadDetail | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  // A failed read of the thread, said in the dialog with Retry; Retry reads it again.
  const [loadError, setLoadError] = useState<unknown>(null);
  const [retries, setRetries] = useState(0);
  // Durable memories this thread produced (via source_trace=conversation:<id>).
  const [threadMemories, setThreadMemories] = useState<MemoryListEntry[]>([]);
  // How many memories the thread produced in all: the panel reads the first 20.
  const [threadMemoriesTotal, setThreadMemoriesTotal] = useState(0);
  // When a thread is opened from a search hit, scroll to + highlight that message.
  // The turn a search hit names: its id and the start of its text, since two agents' turns
  // in one thread can share an id.
  const [highlight, setHighlight] = useState<{ msgId: string; excerpt: string } | null>(null);
  // Cursor for the next page of messages (conversation.list). Null
  // means "no more" — either we've walked to the end or the initial
  // fetch fit under the limit. The "Load more" button only renders
  // when this is set.
  const [moreCursor, setMoreCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // The open thread's reads (detail, memories, "Load more"); aborted when another thread
  // opens, the dialog closes or its opener unmounts, so thread A's answers never land
  // under thread B.
  const threadReads = useRef<AbortController | null>(null);

  useEffect(() => {
    threadReads.current?.abort();
    threadReads.current = null;
    setThreadDetail(null);
    setMoreCursor(null);
    setLoadingMore(false);
    setThreadMemories([]);
    setThreadMemoriesTotal(0);
    setLoadError(null);
    if (!thread) {
      setThreadLoading(false);
      setHighlight(null);
      return;
    }
    const ctl = new AbortController();
    threadReads.current = ctl;
    const { signal } = ctl;
    const { thread_id, namespace, hit } = thread;
    setHighlight(hit ? { msgId: hit.msg_id, excerpt: hit.content_excerpt } : null);
    setThreadLoading(true);
    // What durable memories did this conversation produce? (provenance link)
    listMemories({ ...identity, namespace }, 20, {
      source_trace: `conversation:${thread_id}`,
      include_profile: true,
      signal,
    })
      .then((r) => {
        if (signal.aborted) return;
        setThreadMemories(r.entries);
        setThreadMemoriesTotal(r.total);
      })
      .catch(() => {
        if (!signal.aborted) setThreadMemories([]);
      });
    (async () => {
      try {
        // Every read asks for "*", every agent's turns, however the thread was opened.
        const d = await getConversationThread({ org: identity.org, namespace }, "*", thread_id, {
          limit: 200,
          signal,
        });
        if (signal.aborted) return;
        setThreadDetail(d);
        // More turns follow: conversation.list pages on from the cursor get issued.
        setMoreCursor(d.next_cursor ?? null);
      } catch (e) {
        if (signal.aborted) return;
        setLoadError(e);
      } finally {
        if (!signal.aborted) setThreadLoading(false);
      }
    })();
    return () => ctl.abort();
    // `identity` is read for the namespace's org; a new thread object is the trigger.
  }, [thread, retries]);

  const highlightedTurn = useMemo(() => {
    if (!highlight || !threadDetail) return null;
    const start = highlight.excerpt.replace(/…$/, "");
    const m = threadDetail.messages.find((t) => t.msg_id === highlight.msgId && t.content.startsWith(start));
    return m ? turnId(m) : null;
  }, [highlight, threadDetail]);

  // Scroll to the turn a search hit pointed at, once the thread loads.
  useEffect(() => {
    if (!highlightedTurn) return;
    const t = setTimeout(
      () => document.getElementById(highlightedTurn)?.scrollIntoView({ block: "center", behavior: "smooth" }),
      60,
    );
    return () => clearTimeout(t);
  }, [highlightedTurn]);

  const focusTurn = useRef<string | null>(null);
  const transcriptRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (focusTurn.current === null) return;
    // The transcript takes focus, not the turn: a tabbable region, so Tab moves on from
    // it inside the dialog; the first new turn ("" when the page brought none) is
    // scrolled into view.
    transcriptRef.current?.focus({ preventScroll: true });
    if (focusTurn.current) document.getElementById(focusTurn.current)?.scrollIntoView?.({ block: "start" });
    focusTurn.current = null;
  }, [threadDetail]);

  const loadMoreMessages = async () => {
    const reads = threadReads.current;
    if (!reads || !thread || !threadDetail || !moreCursor || loadingMore) return;
    const { signal } = reads;
    const { thread_id, namespace } = thread;
    setLoadingMore(true);
    try {
      const r = await listConversationMessages({ org: identity.org, namespace }, "*", thread_id, {
        cursor: moreCursor,
        limit: 200,
        signal,
      });
      if (signal.aborted) return;
      // Merge into the detail as it is now, and only if it is still this thread's. New
      // rows append (server returns chronological order; we render in-place).
      setThreadDetail((prev) => {
        if (!prev || prev.thread_id !== thread_id) return prev;
        return { ...prev, messages: [...prev.messages, ...r.rows] };
      });
      const next = r.next_cursor;
      setMoreCursor(next);
      // The last page removes Load more; its focus moves to the transcript, at the first
      // turn the page brought, if any.
      if (!next) focusTurn.current = r.rows.length > 0 ? turnId(r.rows[0]) : "";
    } catch (e) {
      if (signal.aborted) return;
      toastError("Load more failed", e);
    } finally {
      if (!signal.aborted) setLoadingMore(false);
    }
  };

  return (
    <Modal
      open={thread !== null}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
      title={
        threadTitle(threadDetail?.title, threadDetail?.messages?.find((m) => m.role === "user")?.content) ||
        thread?.knownTitle ||
        UNTITLED_THREAD
      }
      className="max-w-[820px] w-full"
      initialFocus="dialog"
    >
      <div className="text-xs text-text-muted font-mono truncate">{thread?.thread_id}</div>
      {thread && thread.namespace !== identity.namespace && (
        <div className="mt-1">
          <ScopeBadge kind="namespace" value={thread.namespace} />
        </div>
      )}
      {threadDetail?.found && (
        <div className="text-xs text-text-muted mt-1 flex items-center gap-2 flex-wrap">
          <span>
            {threadDetail.msg_count} message{threadDetail.msg_count === 1 ? "" : "s"}
          </span>
          {threadDetail.agent && (
            <>
              <span>·</span>
              <AgentBadge agent={threadDetail.agent} />
            </>
          )}
          {threadDetail.updated_at && (
            <>
              <span>·</span>
              <span>
                updated <ListTime time={threadDetail.updated_at} />
              </span>
            </>
          )}
        </div>
      )}
      {actions && <div className="mt-2">{actions}</div>}
      <div className="mt-4">
        {thread && <SessionBriefPanel org={identity.org} namespace={thread.namespace} sessionId={thread.thread_id} />}
        {threadMemories.length > 0 && (
          <div className="mb-4 p-3 rounded-lg bg-surface-2 border border-border">
            <SectionHeading
              label="Memories this thread produced"
              count={Math.max(threadMemoriesTotal, threadMemories.length)}
            />
            <div className="flex flex-wrap gap-1.5">
              {threadMemories.map((m) => (
                <Chip key={m.memory_key} label={m.memory_key} mono />
              ))}
            </div>
            {threadMemoriesTotal > threadMemories.length && (
              <p className="mt-2 text-xs text-text-muted">{`Showing the newest ${threadMemories.length}.`}</p>
            )}
          </div>
        )}
        {threadLoading && <Loading label="Loading thread" />}
        {!threadLoading && loadError !== null && (
          <ListState kind="error" things="the thread" error={loadError} onRetry={() => setRetries((n) => n + 1)} />
        )}
        {!threadLoading && threadDetail && !threadDetail.found && (
          <div className="text-center text-sm text-text-muted py-12">Thread not found. It may have been deleted.</div>
        )}
        {!threadLoading && threadDetail && threadDetail.found && threadDetail.messages.length === 0 && (
          <div className="text-center text-sm text-text-muted py-12">Thread exists but contains no messages.</div>
        )}
        {!threadLoading && threadDetail && threadDetail.messages.length > 0 && (
          <section
            ref={transcriptRef}
            aria-label="Transcript"
            // biome-ignore lint/a11y/noNoninteractiveTabindex: the last Load more hands focus here; Radix's focus trap wraps only from a tabbable element
            tabIndex={0}
            className="space-y-2.5"
          >
            {threadDetail.messages.map((m) => {
              const meta = ROLE_META[m.role] || ROLE_META.assistant;
              const label =
                m.role === "assistant" ? displayAgent(m.agent || threadDetail.agent || "assistant") : meta.label;
              const highlighted = turnId(m) === highlightedTurn;
              return (
                <div
                  key={turnId(m)}
                  id={turnId(m)}
                  data-msg-id={m.msg_id}
                  data-highlighted={highlighted || undefined}
                  className={`thread-turn rounded-lg px-3 py-2 ${meta.card}`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <RoleBadge label={label} />
                    {typeof m.tokens === "number" && m.tokens > 0 && (
                      <span className="text-xs text-text-muted">· {m.tokens} tok</span>
                    )}
                    <ListTime time={m.ts} quiet className="text-xs text-text-muted ml-auto" />
                  </div>
                  {m.reasoning ? <ReasoningDisclosure text={m.reasoning} /> : null}
                  <MessageBody content={m.content} muted={meta.muted} />
                </div>
              );
            })}
            {moreCursor && (
              <div className="pt-3 flex justify-center">
                <BusyButton type="secondary" htmlType="button" busy={loadingMore} onClick={loadMoreMessages}>
                  {`Load more (${threadDetail.msg_count - threadDetail.messages.length} remaining)`}
                </BusyButton>
              </div>
            )}
          </section>
        )}
      </div>
    </Modal>
  );
}
