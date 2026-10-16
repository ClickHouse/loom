import { useEffect, useRef, useState } from "react";
import { Inbox } from "lucide-react";
import { taskCounts, type InboxCount } from "../api/tasks";
import { useVisiblePoll } from "../lib/hooks";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCall } from "../lib/canCall";
import { roleOnNamespace } from "../lib/namespaceRole";
import { isMemoryNamespace } from "../lib/namespace";
import { attentionByInbox, bellCount } from "../lib/taskBell";
import { relativeTime } from "../lib/format";

// A remembered badge answer is shown until the refresh replaces it — one poll interval.
const SESSION_TTL_MS = 120_000;

/**
 * The task inbox bell: how many tasks in this namespace's inboxes wait for a person —
 * review items in triage, and tasks blocked on a human (a block on the sender or an
 * external event is listed, not counted). One `task.counts` call for the namespace
 * (numbers per inbox × status, never rows), on idle after first paint and then every two
 * minutes while the tab is visible; only for a role the registry lets read it. The
 * dropdown lists the inboxes with something waiting and opens the Tasks view on one.
 */
export function TaskInboxBell() {
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  const [counts, setCounts] = useState<InboxCount[]>([]);
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const dropdownRef = useRef<HTMLDivElement | null>(null);

  const ns = identity?.namespace ?? "";
  const roleHere = roleOnNamespace(whoami, myNamespaces, ns);
  const mayRead = !!identity && isMemoryNamespace(ns, myNamespaces) && canCall("task.counts", roleHere, whoami);
  const sessionKey = identity ? `taskbell:${identity.org}/${ns}` : "";
  // The namespace on screen now; a count read for the one before must not replace its badge.
  const sessionKeyRef = useRef(sessionKey);

  const refresh = async () => {
    if (!identity || !mayRead) return;
    try {
      const rows = await taskCounts({ org: identity.org, namespace: identity.namespace });
      writeSession(sessionKey, rows);
      if (sessionKeyRef.current === sessionKey) setCounts(rows);
    } catch {
      /* fail-quiet: the badge just stays hidden */
    }
  };

  useEffect(() => {
    sessionKeyRef.current = sessionKey;
    if (!sessionKey) return;
    const last = readSession<InboxCount[]>(sessionKey, SESSION_TTL_MS);
    setCounts(last ?? []);
  }, [sessionKey]);
  useAfterPaint(
    () => {
      void refresh();
    },
    [identity?.org, ns, mayRead],
    mayRead,
  );
  useVisiblePoll(refresh, 120_000, mayRead);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target instanceof Node ? e.target : null;
      if (triggerRef.current?.contains(t) || dropdownRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!identity || !mayRead) return null;

  const total = bellCount(counts);
  const inboxes = attentionByInbox(counts);

  const openInbox = (inbox: string) => {
    setOpen(false);
    // The Tasks view owns `?inbox=` (state.ts VIEW_QUERY_KEYS), so setting
    // it before the navigation carries it into the view's URL.
    try {
      const u = new URL(window.location.href);
      u.searchParams.set("inbox", inbox);
      u.searchParams.delete("task");
      window.history.replaceState(null, "", u.toString());
    } catch {
      /* no URL to carry it in */
    }
    useStore.getState().setView("tasks");
  };

  return (
    <div className="relative">
      <button
        type="button"
        ref={triggerRef}
        onClick={() => setOpen((v) => !v)}
        className="h-8 w-8 inline-flex items-center justify-center text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)] rounded-md transition-colors relative"
        title={
          total > 0 ? `${total} task${total === 1 ? "" : "s"} waiting for a person` : "No tasks waiting for a person"
        }
        aria-label="Tasks waiting for a person"
        aria-expanded={open}
      >
        <Inbox size={15} />
        {total > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-[16px] px-1 inline-flex items-center justify-center text-2xs font-semibold bg-[var(--color-warn)] text-[var(--color-on-accent)] rounded-full">
            {total > 99 ? "99+" : total}
          </span>
        )}
      </button>
      {open && (
        <div
          ref={dropdownRef}
          className="absolute right-0 top-full mt-1 z-50 w-[min(400px,92vw)] max-h-[480px] overflow-y-auto rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] shadow-xl py-1"
        >
          <div className="px-4 py-2.5 border-b border-[var(--color-border)] text-xs text-[var(--color-text-subtle)] font-medium">
            Tasks waiting · {total}
          </div>
          {inboxes.length === 0 && (
            <div className="px-4 py-6 text-sm text-[var(--color-text-muted)] text-center">
              Nothing waits for you.
              <div className="text-2xs text-[var(--color-text-subtle)] mt-1.5">
                Review items in triage and blocked tasks in the inboxes of <span className="font-mono">{ns}</span> show
                up here.
              </div>
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  useStore.getState().setView("tasks");
                }}
                className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-[var(--color-border)] hover:bg-[var(--color-surface-2)] text-[var(--color-text)]"
              >
                Open Tasks →
              </button>
            </div>
          )}
          {inboxes.map((a) => (
            <button
              type="button"
              key={a.inbox}
              onClick={() => openInbox(a.inbox)}
              className="w-full text-left px-4 py-2.5 hover:bg-[var(--color-surface-2)] flex items-start gap-3 transition-colors"
            >
              <Inbox size={13} className="text-[var(--color-text-muted)] shrink-0 mt-0.5" aria-hidden="true" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium font-mono break-all">{a.inbox}</div>
                <div className="text-2xs text-[var(--color-text-muted)] mt-0.5">
                  {[
                    a.triage > 0 ? `${a.triage} in triage` : "",
                    a.blockedHuman > 0 ? `${a.blockedHuman} blocked on a person` : "",
                    a.blockedOther > 0 ? `${a.blockedOther} blocked on a sender or external event` : "",
                    a.oldest ? `oldest ${relativeTime(a.oldest)}` : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              </div>
              <span className="text-2xs text-[var(--color-text-subtle)] mt-1 shrink-0">Open →</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
