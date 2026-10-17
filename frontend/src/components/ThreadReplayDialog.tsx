import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import { Chip, ExternalLink, SectionHeading } from "./ui";
import { Modal } from "./ui/Modal";
import { toast } from "./Toast";
import { absoluteTime, displayAgent, relativeTime, humanizeError } from "../lib/format";
import { CopyButton } from "./CopyButton";
import { SessionBriefPanel } from "./SessionBriefPanel";
import { ScopeBadge } from "./ScopeBadge";

/** The thread a replay shows: its id and namespace, the agent its opener named (for a
 *  replica of the previous release) and the search hit it was opened from, if any. */
export interface ReplayThread {
  thread_id: string;
  namespace: string;
  knownAgent?: string;
  hit?: ConversationHit;
}

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

// A turn's element id: a msg_id is unique per agent only, and a thread read across
// agents can hold two agents' turns under one id.
function turnId(m: ConversationMessage): string {
  return `msg-${m.agent ?? ""}-${m.msg_id}`;
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
  // Durable memories this thread produced (via source_trace=conversation:<id>).
  const [threadMemories, setThreadMemories] = useState<MemoryListEntry[]>([]);
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
    if (!thread) {
      setThreadLoading(false);
      setHighlight(null);
      return;
    }
    const ctl = new AbortController();
    threadReads.current = ctl;
    const { signal } = ctl;
    const { thread_id, namespace, knownAgent, hit } = thread;
    setHighlight(hit ? { msgId: hit.msg_id, excerpt: hit.content_excerpt } : null);
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
    (async () => {
      try {
        // Every read asks for "*", every agent's turns, however the thread was opened.
        const read = (agent: string) =>
          getConversationThread({ org: identity.org, namespace }, agent, thread_id, { limit: 200, signal });
        let d = await read("*");
        // A replica of the previous release reads "*" as a literal agent; the thread
        // list's agent still opens the thread there. Drop with the next release.
        if (!d.found && knownAgent && !signal.aborted) {
          d = await read(knownAgent);
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
    })();
    return () => ctl.abort();
    // `identity` is read for the namespace's org; a new thread object is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread]);

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
    if (!focusTurn.current) return;
    // The transcript takes focus, not the turn: a tabbable region, so Tab moves on from
    // it inside the dialog; the first new turn is scrolled into view.
    transcriptRef.current?.focus({ preventScroll: true });
    document.getElementById(focusTurn.current)?.scrollIntoView?.({ block: "start" });
    focusTurn.current = null;
  }, [threadDetail]);

  const loadMoreMessages = async () => {
    const reads = threadReads.current;
    if (!reads || !thread || !threadDetail || !moreCursor || loadingMore) return;
    const { signal } = reads;
    const { thread_id, namespace, knownAgent } = thread;
    setLoadingMore(true);
    try {
      const page = (as: string) =>
        listConversationMessages({ org: identity.org, namespace }, as, thread_id, {
          cursor: moreCursor,
          limit: 200,
          signal,
        });
      let r = await page("*");
      // A replica of the previous release answers "*" with no rows; the opener's agent
      // still pages there. Drop with the next release.
      const fellBack = r.rows.length === 0 && !!knownAgent && !signal.aborted;
      if (fellBack && knownAgent) r = await page(knownAgent);
      if (signal.aborted) return;
      // Merge into the detail as it is now, and only if it is still this thread's. New
      // rows append (server returns chronological order; we render in-place).
      setThreadDetail((prev) => {
        if (!prev || prev.thread_id !== thread_id) return prev;
        return { ...prev, messages: [...prev.messages, ...r.rows] };
      });
      // A fallback page is one agent's: its cursor would resume every agent's walk past the
      // others' turns, so paging stops there and the dialog says how much it shows.
      const next = fellBack ? null : r.next_cursor;
      setMoreCursor(next);
      // The last page removes Load more; its focus moves to the transcript, at the first
      // turn the page brought.
      if (!next && r.rows.length > 0) focusTurn.current = turnId(r.rows[0]);
    } catch (e) {
      if (signal.aborted) return;
      toast(`Load more failed: ${humanizeError(e)}`, "error");
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
      title={threadDetail?.title || "Thread"}
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
      {actions && <div className="mt-2">{actions}</div>}
      <div className="mt-4">
        {thread && <SessionBriefPanel org={identity.org} namespace={thread.namespace} sessionId={thread.thread_id} />}
        {threadMemories.length > 0 && (
          <div className="mb-4 p-3 rounded-lg bg-surface-2 border border-border">
            <SectionHeading label="Memories this thread produced" count={threadMemories.length} />
            <div className="flex flex-wrap gap-1.5">
              {threadMemories.map((m) => (
                <Chip key={m.memory_key} label={m.memory_key} mono />
              ))}
            </div>
          </div>
        )}
        <p role="status" className="sr-only">
          {threadLoading ? "Loading…" : ""}
        </p>
        {threadLoading && (
          <div className="text-center text-sm text-text-muted py-12" aria-hidden="true">
            Loading…
          </div>
        )}
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
                  className={`rounded-lg px-3 py-2 ${meta.rail} ${meta.card} ${highlighted ? "ring-2 ring-accent" : ""}`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-medium text-text-muted">{label}</span>
                    {typeof m.tokens === "number" && m.tokens > 0 && (
                      <span className="text-xs text-text-muted">· {m.tokens} tok</span>
                    )}
                    <time dateTime={m.ts} className="text-xs text-text-muted ml-auto tabular">
                      {relativeTime(m.ts)}
                      <span className="sr-only"> ({absoluteTime(m.ts)})</span>
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
                {`Showing ${threadDetail.messages.length} of ${threadDetail.msg_count} messages.`}
              </p>
            )}
          </section>
        )}
      </div>
    </Modal>
  );
}
