import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Icon } from "@clickhouse/click-ui/Icon";
import {
  acceptTask,
  claimTask,
  listInboxes,
  listTasks,
  resumeTask,
  taskCounts,
  unblockTask,
  type Inbox,
  type InboxCount,
  type TaskEvent,
  type TaskListRow,
  type TaskRow,
} from "../api/tasks";
import { useStore, useIdentity } from "../lib/state";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { useVisiblePoll } from "../lib/hooks";
import {
  ACTION_LABEL,
  DIALOG_ACTIONS,
  GROUPS,
  NO_FILTERS,
  actionsFor,
  attentionCounts,
  dropAction,
  groupTasks,
  kindsOf,
  latestProgressNote,
  passesFilters,
  type Attention,
  type Filters,
  type Group,
  type TaskAction,
} from "../lib/tasks";
import { SkeletonCard } from "../components/Skeleton";
import { Kbd, Tip } from "../components/ui";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { toast, toastError } from "../components/Toast";
import { humanizeError } from "../lib/format";
import { PageContainer } from "../layout/PageContainer";
import { AttentionTiles } from "./tasks/AttentionTiles";
import { ALL_INBOXES, InboxRail, railCounts } from "./tasks/InboxRail";
import { TaskList, type RowContext } from "./tasks/TaskList";
import { TaskBoard } from "./tasks/TaskBoard";
import { TaskPanel } from "./tasks/TaskPanel";
import type { ReasonAction } from "../flows/TaskActionDialog";

// Dialogs load on the click that opens them.
const TaskEditDialog = lazy(() => import("../flows/TaskEditDialog").then((m) => ({ default: m.TaskEditDialog })));
const TaskActionDialog = lazy(() => import("../flows/TaskActionDialog").then((m) => ({ default: m.TaskActionDialog })));
const InboxSettingsDialog = lazy(() =>
  import("../flows/InboxSettingsDialog").then((m) => ({ default: m.InboxSettingsDialog })),
);

/**
 * Tasks — the inbox plane's page (redesigned).
 *
 * Attention first: four tiles count what needs a person (triage, blocked on
 * a person, failed, a lease ending within ten minutes) and filter on a
 * click. Inboxes are folders in a left rail with to-do and running counts;
 * the picked one is the page's scope (`?inbox=`). Rows group by what
 * happens next — Triage, To do, In progress, Waiting, Closed — as a list or
 * a board; each row carries the one line that matters for its state and,
 * in Triage, inline Accept / Won't do. A row opens the document-shaped
 * panel beside the list; its footer holds the status's actions.
 *
 * Reads on load: `inbox.list` once, `task.counts` once (the rail), and
 * `task.list` once for the picked inbox (the list's projection); a 60 s poll while
 * visible. "All inboxes" reads one `task.list` per inbox (the op needs an inbox), on that
 * click only. The panel reads `task.get` + `task.history` when it opens.
 *
 * Keys: j / k move, Enter opens, a accepts, s starts, c marks done, x won't do, d defers,
 * / focuses the kind filter, Esc closes the panel.
 */
type Mode = "list" | "board";

function ViewLabel({ icon, text }: { icon: "list-bulleted" | "services"; text: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <Icon name={icon} size="xs" aria-hidden="true" />
      {text}
    </span>
  );
}

function readQuery(key: string): string | null {
  try {
    return new URLSearchParams(window.location.search).get(key);
  } catch {
    return null;
  }
}

function writeQuery(key: string, value: string | null): void {
  try {
    const u = new URL(window.location.href);
    if (value) u.searchParams.set(key, value);
    else u.searchParams.delete(key);
    window.history.replaceState(null, "", u.toString());
  } catch {
    /* nothing to write into */
  }
}

function inEditable(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/** A `task.get` answer rather than a list row: every field the list's projection leaves
 *  out is present. Only a full task can be edited (the edit keeps the brief's other keys). */
function isFullTask(t: TaskListRow | TaskRow): t is TaskRow {
  return (
    t.brief !== undefined &&
    t.outcome !== undefined &&
    t.origin !== undefined &&
    t.attempt !== undefined &&
    t.fence !== undefined &&
    t.last_actor !== undefined &&
    t.org !== undefined &&
    t.namespace !== undefined
  );
}

export function Tasks() {
  const identity = useIdentity();
  const whoami = useStore((s) => s.whoami);
  const role = useNamespaceRole(identity.namespace);
  const caller = useMemo(
    () => ({ org: identity.org, namespace: identity.namespace }),
    [identity.org, identity.namespace],
  );
  const mayWrite = canCall("task.create", role, whoami);
  const mayAdmin = canCall("inbox.register", role, whoami);

  const [inboxes, setInboxes] = useState<Inbox[] | null>(null);
  const [inboxError, setInboxError] = useState<string | null>(null);
  const [counts, setCounts] = useState<InboxCount[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [rows, setRows] = useState<TaskListRow[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [rowsError, setRowsError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>(() => {
    try {
      return localStorage.getItem("loom_tasks_mode") === "board" ? "board" : "list";
    } catch {
      return "list";
    }
  });
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [openRow, setOpenRow] = useState<TaskListRow | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [progressNotes, setProgressNotes] = useState<ReadonlyMap<string, string>>(new Map());
  const [creating, setCreating] = useState(false);
  const [settings, setSettings] = useState<"register" | "edit" | null>(null);
  const [dialog, setDialog] = useState<{ action: ReasonAction; task: TaskListRow | TaskRow } | null>(null);
  const [editing, setEditing] = useState<TaskRow | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // Only the latest task.counts may write the counts: a namespace switch while A's read is
  // in flight drops A's answer, so B's tiles never show A's numbers.
  const countsSeq = useRef(0);
  const refreshCounts = useCallback(() => {
    const seq = ++countsSeq.current;
    taskCounts(caller)
      .then((c) => {
        if (seq === countsSeq.current) setCounts(c);
      })
      .catch(() => {
        if (seq === countsSeq.current) setCounts((c) => c ?? []);
      });
  }, [caller]);

  // Only the latest task.list may write the rows: picking B while A's read (or A's poll)
  // is in flight drops A's answer. A refresh after an action is the latest, so it lands.
  const rowsSeq = useRef(0);

  useEffect(() => {
    // A namespace switch restores only its own inboxes, and drops any row read in flight.
    let live = true;
    rowsSeq.current += 1;
    setInboxes(null);
    setInboxError(null);
    setCounts(null);
    setPicked(null);
    const want = readQuery("inbox");
    listInboxes(caller)
      .then((list) => {
        if (!live) return;
        setInboxes(list);
        const known =
          want && list.some((i) => i.inbox === want)
            ? want
            : want === ALL_INBOXES && list.length > 1
              ? ALL_INBOXES
              : (list[0]?.inbox ?? null);
        setPicked(known);
      })
      .catch((e) => {
        if (!live) return;
        setInboxError(humanizeError(e));
        setInboxes([]);
      });
    refreshCounts();
    return () => {
      live = false;
    };
  }, [caller, refreshCounts]);

  const refresh = useCallback(() => {
    if (!picked || !inboxes) return;
    const seq = ++rowsSeq.current;
    const targets = picked === ALL_INBOXES ? inboxes.map((i) => i.inbox) : [picked];
    Promise.all(targets.map((inbox) => listTasks(caller, { inbox, limit: 200 })))
      .then((results) => {
        if (seq !== rowsSeq.current) return;
        setRows(results.flatMap((r) => r.tasks));
        setTruncated(results.some((r) => r.truncated));
        setRowsError(null);
      })
      .catch((e) => {
        if (seq !== rowsSeq.current) return;
        setRowsError(humanizeError(e));
        setRows([]);
      });
  }, [caller, picked, inboxes]);

  useEffect(() => {
    if (!picked) return;
    setRows(null);
    setRowsError(null);
    writeQuery("inbox", picked);
    refresh();
  }, [picked, refresh]);
  useVisiblePoll(refresh, 60_000, !!picked);

  // The lease bars and ages tick while something runs.
  const anyRunning = !!rows?.some((r) => r.status === "in_progress");
  useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, [anyRunning]);

  // A `?task=` deep link opens its row once the list has it.
  useEffect(() => {
    if (!rows || openRow) return;
    const want = readQuery("task");
    if (!want) return;
    const r = rows.find((x) => x.task_id === want);
    if (r) {
      setOpenRow(r);
      setSelectedId(r.task_id);
    }
  }, [rows, openRow]);

  // The open row follows the list: a refresh after an action carries the
  // new status into the panel's header before task.get answers.
  useEffect(() => {
    if (!rows || !openRow) return;
    const r = rows.find((x) => x.task_id === openRow.task_id);
    if (r && r !== openRow) setOpenRow(r);
  }, [rows, openRow]);

  const byInbox = useMemo(() => new Map((inboxes ?? []).map((i) => [i.inbox, i])), [inboxes]);
  const ownerAgentOf = useCallback((inbox: string) => byInbox.get(inbox)?.owner_agent, [byInbox]);
  const leaseSecondsOf = useCallback((inbox: string) => byInbox.get(inbox)?.default_lease_seconds || 1800, [byInbox]);
  const inbox = picked && picked !== ALL_INBOXES ? (byInbox.get(picked) ?? null) : null;

  const attention = useMemo(() => attentionCounts(rows ?? [], now), [rows, now]);
  const kinds = useMemo(() => kindsOf(rows ?? []), [rows]);
  const visible = useMemo(
    () => (rows ?? []).filter((r) => passesFilters(r, filters, ownerAgentOf, now)),
    [rows, filters, ownerAgentOf, now],
  );
  const groups = useMemo(() => groupTasks(visible), [visible]);
  const showClosed = filters.showClosed || filters.attention === "failed";
  const ordered = useMemo(
    () => GROUPS.flatMap((g) => (g === "closed" && !showClosed ? [] : groups[g])),
    [groups, showClosed],
  );
  const filtering = !!(filters.attention || filters.kind || filters.poster);

  const setModeAndRemember = (m: Mode) => {
    setMode(m);
    try {
      localStorage.setItem("loom_tasks_mode", m);
    } catch {
      /* fine */
    }
  };
  const pick = (i: string) => {
    setPicked(i);
    setFilters(NO_FILTERS);
    setSelectedId(null);
    setOpenRow(null);
    writeQuery("task", null);
  };
  const toggleTile = (k: Attention) => setFilters((f) => ({ ...f, attention: f.attention === k ? null : k }));

  const openTask = useCallback((r: TaskListRow) => {
    setSelectedId(r.task_id);
    setOpenRow(r);
    setReloadKey(0);
    writeQuery("task", r.task_id);
  }, []);
  const closeTask = useCallback(() => {
    setOpenRow(null);
    writeQuery("task", null);
  }, []);

  const afterChange = useCallback(
    (t: TaskRow, replacement?: TaskRow) => {
      setRows((cur) => {
        if (!cur) return cur;
        let next = cur.map((r) => (r.task_id === t.task_id ? { ...r, ...t } : r));
        if (replacement && (picked === ALL_INBOXES || replacement.inbox === picked)) next = [replacement, ...next];
        return next;
      });
      setReloadKey((k) => k + 1);
      refresh();
      refreshCounts();
    },
    [picked, refresh, refreshCounts],
  );

  const onAction = useCallback(
    async (action: TaskAction, task: TaskListRow | TaskRow) => {
      if (!mayWrite || !actionsFor(task.status).includes(action)) return;
      if (action === "edit") {
        if (isFullTask(task)) setEditing(task);
        return;
      }
      // Resume and start never open the dialog; ruling them out narrows the action to a ReasonAction.
      if (action !== "resume" && action !== "start" && DIALOG_ACTIONS.has(action)) {
        setDialog({ action, task });
        return;
      }
      try {
        const r =
          action === "accept"
            ? await acceptTask(caller, task.task_id)
            : action === "start"
              ? await claimTask(caller, task.task_id)
              : action === "unblock"
                ? await unblockTask(caller, task.task_id)
                : await resumeTask(caller, task.task_id);
        toast(`${ACTION_LABEL[action]}: done`, "ok");
        afterChange(r.task);
      } catch (e) {
        toastError(ACTION_LABEL[action], e);
      }
    },
    [mayWrite, caller, afterChange],
  );

  const onHistory = useCallback((taskId: string, events: TaskEvent[]) => {
    const note = latestProgressNote(events);
    setProgressNotes((m) => (m.get(taskId) === note ? m : new Map(m).set(taskId, note)));
  }, []);

  // Moving the selection (j / k here, the arrow keys in the list) follows with the open
  // panel and takes the focus to the row.
  const selectRow = useCallback(
    (r: TaskListRow) => {
      setSelectedId(r.task_id);
      if (openRow) openTask(r);
      const el = document.querySelector<HTMLElement>(`[data-task-id="${r.task_id}"]`);
      el?.scrollIntoView?.({ block: "nearest" });
      el?.focus({ preventScroll: true });
    },
    [openRow, openTask],
  );

  // Keys: j / k over the visible rows, Enter opens, a / s / c / x / d act on the
  // selected row, / focuses the kind filter, Esc closes the panel. Ignored
  // while typing or while a dialog is open.
  const orderedRef = useRef(ordered);
  orderedRef.current = ordered;
  const stateRef = useRef({ selectedId, openRow, onAction, openTask, closeTask, selectRow });
  stateRef.current = { selectedId, openRow, onAction, openTask, closeTask, selectRow };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || inEditable(e.target)) return;
      if (document.querySelector('[role="dialog"]')) return;
      const { selectedId: sel, openRow: open, onAction: act, openTask: openT, closeTask: closeT } = stateRef.current;
      const list = orderedRef.current;
      const i = list.findIndex((r) => r.task_id === sel);
      const move = (to: number) => {
        const r = list[Math.max(0, Math.min(list.length - 1, to))];
        if (r) stateRef.current.selectRow(r);
      };
      const row = i >= 0 ? list[i] : null;
      switch (e.key) {
        case "j":
          e.preventDefault();
          move(i < 0 ? 0 : i + 1);
          break;
        case "k":
          e.preventDefault();
          move(i < 0 ? 0 : i - 1);
          break;
        case "Enter":
          if (row && e.target === document.body) {
            e.preventDefault();
            openT(row);
          }
          break;
        case "Escape":
          if (open) {
            e.preventDefault();
            closeT();
          }
          break;
        case "a":
          if (row) {
            e.preventDefault();
            void act("accept", row);
          }
          break;
        case "s":
          if (row) {
            e.preventDefault();
            void act("start", row);
          }
          break;
        case "c":
          if (row) {
            e.preventDefault();
            void act("complete", row);
          }
          break;
        case "x":
          if (row) {
            e.preventDefault();
            void act("wont_do", row);
          }
          break;
        case "d":
          if (row) {
            e.preventDefault();
            void act("defer", row);
          }
          break;
        case "/": {
          e.preventDefault();
          document.querySelector<HTMLElement>("#tq-kind-chips button")?.focus();
          break;
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const ctx: RowContext = useMemo(
    () => ({
      now,
      ownerAgentOf,
      leaseSecondsOf,
      progressNotes,
      selectedId,
      canWrite: mayWrite,
      onOpen: openTask,
      onSelect: selectRow,
      onAccept: (r) => {
        void onAction("accept", r);
      },
      onWontDo: (r) => {
        void onAction("wont_do", r);
      },
      onDrop: (r: TaskListRow, to: Group) => {
        const a = dropAction(r.status, to);
        if (a) void onAction(a, r);
      },
    }),
    [now, ownerAgentOf, leaseSecondsOf, progressNotes, selectedId, mayWrite, openTask, selectRow, onAction],
  );

  const createInbox = inbox?.inbox ?? inboxes?.[0]?.inbox ?? null;

  return (
    <PageContainer>
      <header className="flex items-center justify-between gap-4 flex-wrap pb-3">
        <div className="flex items-center gap-2.5 flex-wrap">
          <h1 className="text-2xl font-semibold tracking-tight inline-flex items-center gap-2.5 m-0">
            <Icon name="email" size="md" aria-hidden="true" /> Tasks
          </h1>
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <ButtonGroup
            aria-label="View"
            selected={mode}
            onClick={(v) => setModeAndRemember(v === "board" ? "board" : "list")}
            options={[
              { value: "list", label: <ViewLabel icon="list-bulleted" text="List" /> },
              { value: "board", label: <ViewLabel icon="services" text="Board" /> },
            ]}
          />
          {mayAdmin && inbox && (
            <Tip content="Inbox settings: posters, disclosure, trust, leases">
              <Button type="empty" htmlType="button" iconLeft="gear" onClick={() => setSettings("edit")}>
                Inbox settings
              </Button>
            </Tip>
          )}
          {mayWrite && createInbox && (
            <Button type="primary" htmlType="button" iconLeft="plus" onClick={() => setCreating(true)}>
              New task
            </Button>
          )}
        </div>
      </header>

      {inboxes === null && (
        <div className="space-y-3">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      )}
      {inboxes && inboxError && <EmptyState icon="email" title="Couldn't load the inboxes" description={inboxError} />}
      {inboxes && !inboxError && inboxes.length === 0 && (
        <EmptyState
          icon="email"
          title="No inbox here yet"
          description={`An inbox is one agent's queue, owned by ${identity.namespace}. An admin of this namespace registers it; writers then pull from it and the trust table says who else may post.`}
          action={mayAdmin ? { label: "Register inbox", onClick: () => setSettings("register") } : undefined}
          details={
            <>
              Admins call <code className="font-mono">inbox.register</code> with the owner agent, the posters allowed to
              write blind into it and the disclosure levels it accepts. Agents then{" "}
              <code className="font-mono">task.pull</code> with a lease; people work the board here.
            </>
          }
        />
      )}

      {inboxes && inboxes.length > 0 && picked && (
        <>
          <AttentionTiles counts={attention} active={filters.attention} onToggle={toggleTile} />
          <div className="tq-body" data-panel={!!openRow}>
            <InboxRail
              inboxes={inboxes}
              counts={counts ? railCounts(counts) : null}
              picked={picked}
              onPick={pick}
              kinds={kinds}
              filters={filters}
              onFilters={setFilters}
              onRegister={mayAdmin ? () => setSettings("register") : undefined}
            />
            <div className="min-w-0">
              {rows === null && (
                <div className="space-y-3">
                  <SkeletonCard />
                  <SkeletonCard />
                  <SkeletonCard />
                </div>
              )}
              {rows && rowsError && <EmptyState icon="email" title="Couldn't load the tasks" description={rowsError} />}
              {rows && !rowsError && visible.length === 0 && (
                <div className="tq-empty">
                  {rows.length === 0 ? "Nothing here yet." : "No tasks match."} Bots post with{" "}
                  <span className="font-mono text-xs">task.create</span>; people use New task.
                  {filtering && (
                    <div className="mt-2">
                      <Button
                        type="secondary"
                        htmlType="button"
                        onClick={() => setFilters((f) => ({ ...NO_FILTERS, showClosed: f.showClosed }))}
                      >
                        Clear filters
                      </Button>
                    </div>
                  )}
                </div>
              )}
              {rows &&
                !rowsError &&
                visible.length > 0 &&
                (mode === "list" ? (
                  <TaskList groups={groups} ctx={ctx} forceOpen={filtering} showClosed={showClosed} />
                ) : (
                  <TaskBoard groups={groups} ctx={ctx} showClosed={showClosed} />
                ))}
              {truncated && (
                <div className="text-xs text-text-subtle px-1 pt-1">Showing the first 200 tasks per inbox.</div>
              )}
              <div className="flex gap-3.5 flex-wrap text-xs text-text-subtle px-1 py-3">
                <span>
                  <Kbd>j</Kbd> <Kbd>k</Kbd> move
                </span>
                <span>
                  <Kbd>enter</Kbd> open
                </span>
                <span>
                  <Kbd>a</Kbd> accept
                </span>
                <span>
                  <Kbd>s</Kbd> start
                </span>
                <span>
                  <Kbd>c</Kbd> done
                </span>
                <span>
                  <Kbd>x</Kbd> won't do
                </span>
                <span>
                  <Kbd>d</Kbd> defer
                </span>
                <span>
                  <Kbd>/</Kbd> filter
                </span>
                <span>
                  <Kbd>esc</Kbd> close
                </span>
              </div>
            </div>
            {openRow && (
              <TaskPanel
                identity={caller}
                row={openRow}
                inboxes={inboxes}
                canWrite={mayWrite}
                now={now}
                reloadKey={reloadKey}
                onClose={closeTask}
                onAction={(a, t) => {
                  void onAction(a, t);
                }}
                onHistory={onHistory}
                onOpenTask={(id) => {
                  const r = rows?.find((x) => x.task_id === id);
                  if (r) openTask(r);
                }}
              />
            )}
          </div>
        </>
      )}

      {creating && createInbox && (
        <Suspense fallback={null}>
          <TaskEditDialog
            open
            onOpenChange={(v) => {
              if (!v) setCreating(false);
            }}
            identity={caller}
            mode={{ kind: "create", inbox: createInbox, inboxes: inboxes ?? [] }}
            onSaved={(t) => {
              if (picked === ALL_INBOXES || t.inbox === picked) {
                refresh();
                refreshCounts();
              } else pick(t.inbox);
            }}
          />
        </Suspense>
      )}
      {editing && (
        <Suspense fallback={null}>
          <TaskEditDialog
            open
            onOpenChange={(v) => {
              if (!v) setEditing(null);
            }}
            identity={caller}
            mode={{ kind: "edit", task: editing }}
            onSaved={(t) => afterChange(t)}
          />
        </Suspense>
      )}
      {dialog && (
        <Suspense fallback={null}>
          <TaskActionDialog
            open
            onOpenChange={(v) => {
              if (!v) setDialog(null);
            }}
            identity={caller}
            action={dialog.action}
            task={dialog.task}
            inboxes={inboxes ?? []}
            onDone={afterChange}
          />
        </Suspense>
      )}
      {settings && (
        <Suspense fallback={null}>
          <InboxSettingsDialog
            open
            onOpenChange={(v) => {
              if (!v) setSettings(null);
            }}
            identity={caller}
            inbox={settings === "edit" ? inbox : null}
            onSaved={(i) => {
              setInboxes((cur) => {
                const list = cur ?? [];
                return list.some((x) => x.inbox === i.inbox)
                  ? list.map((x) => (x.inbox === i.inbox ? i : x))
                  : [...list, i];
              });
              if (settings === "register") pick(i.inbox);
            }}
          />
        </Suspense>
      )}
    </PageContainer>
  );
}
