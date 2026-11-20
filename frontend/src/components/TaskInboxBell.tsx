import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Popover } from "@clickhouse/click-ui/Popover";
import { IconButton } from "./IconButton";
import { CountBadge } from "./CountBadge";
import { taskCounts, type InboxCount } from "../api/tasks";
import { useVisiblePoll } from "../lib/hooks";
import { readSession, useAfterPaint, writeSession } from "../lib/afterPaint";
import { useStore } from "../lib/state";
import { canCall } from "../lib/canCall";
import { roleOnNamespace } from "../lib/namespaceRole";
import { isMemoryNamespace } from "../lib/namespace";
import { bellCount } from "../lib/taskBell";
import { PopoverTrigger } from "./ui/Menu";
const TaskInboxList = lazy(() => import("./TaskInboxList").then((m) => ({ default: m.TaskInboxList })));

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
  // The list's chunk loads on the first open.
  const [opened, setOpened] = useState(false);

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

  const tip =
    total > 0 ? `${total} task${total === 1 ? "" : "s"} waiting for a person` : "No tasks waiting for a person";
  return (
    <Popover
      open={open}
      onOpenChange={(v) => {
        setOpen(v);
        if (v) setOpened(true);
      }}
    >
      <span className="relative inline-flex">
        <PopoverTrigger>
          <IconButton
            icon="email"
            label={total > 0 ? `Tasks waiting for a person, ${total}` : "Tasks waiting for a person"}
            tooltip={tip}
          />
        </PopoverTrigger>
        {/* Neutral: triage items and blocks on a person wait, they have not failed. */}
        {total > 0 && <CountBadge count={total} />}
      </span>
      {opened && (
        <Suspense fallback={null}>
          <TaskInboxList counts={counts} namespace={ns} close={() => setOpen(false)} />
        </Suspense>
      )}
    </Popover>
  );
}
