import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import { Popover } from "@clickhouse/click-ui/Popover";
import { IconButton } from "./IconButton";
import { CountBadge } from "./PendingApprovalsBell";
import { taskCounts, type InboxCount } from "../api/tasks";
import { useVisiblePoll } from "../lib/hooks";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCall } from "../lib/canCall";
import { roleOnNamespace } from "../lib/namespaceRole";
import { isMemoryNamespace } from "../lib/namespace";
import { attentionByInbox, bellCount } from "../lib/taskBell";
import { relativeTime } from "../lib/format";
import { PopoverTrigger } from "./ui/Menu";

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
  const headingId = useId();
  const identity = useStore((s) => s.identity);
  const whoami = useStore((s) => s.whoami);
  const myNamespaces = useStore((s) => s.myNamespaces);
  const [counts, setCounts] = useState<InboxCount[]>([]);
  const [open, setOpen] = useState(false);

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

  const tip =
    total > 0 ? `${total} task${total === 1 ? "" : "s"} waiting for a person` : "No tasks waiting for a person";
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <span className="relative inline-flex">
        <PopoverTrigger>
          <IconButton
            icon="email"
            label={total > 0 ? `Tasks waiting for a person, ${total}` : "Tasks waiting for a person"}
            tooltip={tip}
          />
        </PopoverTrigger>
        {total > 0 && <CountBadge count={total} state="warning" />}
      </span>
      <Popover.Content
        align="end"
        aria-labelledby={headingId}
        className="w-[min(400px,92vw)] max-h-[480px] overflow-y-auto"
      >
        <h2 id={headingId} className="section-label pb-2 border-b border-border">
          Tasks waiting · {total}
        </h2>
        {inboxes.length === 0 && (
          <div className="py-6 text-sm text-text-muted text-center">
            Nothing waits for you.
            <div className="text-xs text-text-subtle mt-1.5">
              Review items in triage and blocked tasks in the inboxes of <span className="font-mono">{ns}</span> show up
              here.
            </div>
            <Button
              type="secondary"
              htmlType="button"
              className="mt-3"
              onClick={() => {
                setOpen(false);
                useStore.getState().setView("tasks");
              }}
            >
              Open tasks
            </Button>
          </div>
        )}
        {inboxes.map((a) => (
          <button
            type="button"
            key={a.inbox}
            onClick={() => openInbox(a.inbox)}
            className="text-left -mx-2 w-[calc(100%+1rem)] px-2 py-2.5 rounded-sm hover:bg-surface-2 flex items-start gap-3 transition-colors"
          >
            <Icon name="email" size="xs" aria-hidden="true" className="mt-1" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium font-mono break-all">{a.inbox}</div>
              <div className="text-xs text-text-muted mt-0.5">
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
            <span className="text-xs text-text-subtle mt-1 shrink-0">Open →</span>
          </button>
        ))}
      </Popover.Content>
    </Popover>
  );
}
