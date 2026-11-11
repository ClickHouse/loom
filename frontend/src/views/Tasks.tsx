import { Suspense, lazy, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Button } from "@clickhouse/click-ui/Button";
import { ButtonGroup } from "@clickhouse/click-ui/ButtonGroup";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { Icon } from "@clickhouse/click-ui/Icon";
import { SearchField } from "@clickhouse/click-ui/SearchField";
import { Select } from "@clickhouse/click-ui/Select";
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
import { setViewParam, useStore, useIdentity } from "../lib/state";
import { canCall } from "../lib/canCall";
import { useNamespaceRole } from "../lib/namespaceRole";
import { useVisiblePoll } from "../lib/hooks";
import { humanizeError } from "../lib/errorWords";
import {
  ACTION_LABEL,
  DIALOG_ACTIONS,
  NO_FILTERS,
  actionDone,
  actionsFor,
  attentionCounts,
  dropAction,
  filterSummary,
  groupTasks,
  kindsOf,
  latestProgressNote,
  menuLabel,
  passesFilters,
  type Attention,
  type Filters,
  type Group,
  type TaskAction,
} from "../lib/tasks";
import { SkeletonCard } from "../components/Skeleton";
import { LoadingSkeleton, PageHeader } from "../components/ui";
import { MenuTrigger } from "../components/ui/Menu";
import { ListState } from "../components/ui/ListState";
import { ListToolbar, ListToolbarSearch } from "../components/ui/ListToolbar";
import type { RowMenuItem } from "../components/ui/RowMenu";
import { IconButton } from "../components/IconButton";
import { EmptyState } from "../components/EmptyState";
import { ScopeBadge } from "../components/ScopeBadge";
import { toast, toastError } from "../components/Toast";
import { PageContainer } from "../layout/PageContainer";
import { AttentionTiles } from "./tasks/AttentionTiles";
import { ANY, FiltersMenu, POSTED_BY } from "./tasks/FiltersMenu";
import { ALL_INBOXES, InboxRail, InboxSelect, railCounts } from "./tasks/InboxRail";
import { TaskList, type RowContext } from "./tasks/TaskList";
import { TaskBoard } from "./tasks/TaskBoard";
import { TaskPanel } from "./tasks/TaskPanel";
import { copyTaskId } from "./tasks/TaskBits";
import type { ReasonAction } from "../flows/TaskActionDialog";
import { shortcutOff } from "../lib/shortcuts";
import { useMedia } from "../lib/useMedia";

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
 * click. Inboxes are folders in a left rail with to-do and running counts (a
 * Select on a narrow page); the picked one is the page's scope (`?inbox=`). The
 * toolbar searches the loaded rows and filters them by kind and poster. Rows
 * group by what happens next — Triage, To do, In progress, Waiting, Closed
 * (folded) — as a list or a board; each row carries the one line that matters
 * for its state, its menu of moves and, in Triage, an inline Accept. A row opens
 * the document-shaped panel beside the list (under 1280 px, a modal sheet over
 * it); the action bar under its header holds the status's moves. After a move
 * made from the list, the focus goes on to the next row.
 *
 * Reads on load: `inbox.list` once, `task.counts` once (the rail), and
 * `task.list` once for the picked inbox (the list's projection); a 60 s poll while
 * visible. "All inboxes" reads one `task.list` per inbox (the op needs an inbox), on that
 * click only. The panel reads `task.get` + `task.history` when it opens.
 *
 * Keys: j / k move, Enter opens, a accepts, s starts, c marks done, x won't do, d defers,
 * / focuses the search, Esc closes the panel.
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

// Below Click UI's xl breakpoint the panel is a modal sheet over the list, not a column
// beside it. The one place the breakpoint lives: styles.css keys on what this decides.
const SHEET_QUERY = "(max-width: 1279px)";
// Below md the inbox rail is a Select, the toolbar's filters sit on a row under it and the
// page draws the list only. Wider than lib/useMedia's PHONE_QUERY.
const RAIL_QUERY = "(max-width: 767px)";

// A row's menu, in this order: the move forward, the other ways out, then the hand-offs.
// Accept is the triage row's inline button; Edit brief needs the full task (the panel).
const ROW_MENU: readonly TaskAction[] = [
  "start",
  "complete",
  "unblock",
  "resume",
  "fail",
  "defer",
  "block",
  "wont_do",
  "reopen",
  "reassign",
  "cancel",
];

/** The task rows the list or the board shows, in their order on screen. */
function shownTaskIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".tq-main [data-task-id]")].map((el) => el.dataset.taskId ?? "");
}

/** The row after this one on screen, else the one before: where the focus goes once it moves on. */
function neighbourOf(taskId: string): string | null {
  const ids = shownTaskIds();
  const i = ids.indexOf(taskId);
  return i < 0 ? null : (ids[i + 1] ?? ids[i - 1] ?? null);
}

/** Where a move was made: a row (its button, its menu, a key) or the open panel. */
type Origin = "list" | "panel";

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
  const openHelp = useStore((s) => s.openHelp);
  const role = useNamespaceRole(identity.namespace);
  const caller = useMemo(
    () => ({ org: identity.org, namespace: identity.namespace }),
    [identity.org, identity.namespace],
  );
  const mayWrite = canCall("task.create", role, whoami);
  const mayAdmin = canCall("inbox.register", role, whoami);

  const [inboxes, setInboxes] = useState<Inbox[] | null>(null);
  const [inboxError, setInboxError] = useState<unknown>(null);
  const [inboxReads, setInboxReads] = useState(0);
  const [counts, setCounts] = useState<InboxCount[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [rows, setRows] = useState<TaskListRow[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [rowsError, setRowsError] = useState<unknown>(null);
  // A failed poll or refresh after a good read keeps the rows and says it could not
  // refresh; only a failed first read is the list's error state.
  const [refreshError, setRefreshError] = useState<unknown>(null);
  const rowsNow = useRef(rows);
  rowsNow.current = rows;
  const [mode, setMode] = useState<Mode>(() => {
    try {
      return localStorage.getItem("loom_tasks_mode") === "board" ? "board" : "list";
    } catch {
      return "list";
    }
  });
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [collapsed, setCollapsed] = useState<Record<Group, boolean>>({
    triage: false,
    todo: false,
    in_progress: false,
    waiting: false,
    closed: true,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [openRow, setOpenRow] = useState<TaskListRow | null>(null);
  const openRowRef = useRef(openRow);
  openRowRef.current = openRow;
  const [reloadKey, setReloadKey] = useState(0);
  const [progressNotes, setProgressNotes] = useState<ReadonlyMap<string, string>>(new Map());
  const [creating, setCreating] = useState(false);
  const [settings, setSettings] = useState<"register" | "edit" | null>(null);
  const [dialog, setDialog] = useState<{
    action: ReasonAction;
    task: TaskListRow | TaskRow;
    origin: Origin;
    /** The row the focus goes on to once the move is made. */
    next: string | null;
  } | null>(null);
  const dialogDone = useRef(false);
  const [editing, setEditing] = useState<TaskRow | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const sheetLayout = useMedia(SHEET_QUERY);
  const narrow = useMedia(RAIL_QUERY);

  // Only the latest task.counts may write the counts: a namespace switch while A's read is
  // in flight drops A's answer, so B's tiles never show A's numbers.
  const countsSeq = useRef(0);
  const refreshCounts = useCallback(() => {
    const seq = ++countsSeq.current;
    taskCounts(caller)
      .then((c) => {
        if (seq === countsSeq.current) setCounts(c);
      })
      // A failed read keeps what the rail had: with none yet the counts stay unknown (not
      // "empty" for inboxes that hold tasks), and the poll below reads them again.
      .catch(() => {});
  }, [caller]);

  // Only the latest task.list may write the rows: picking B while A's read (or A's poll)
  // is in flight drops A's answer. A refresh after an action is the latest, so it lands.
  const rowsSeq = useRef(0);
  // Another namespace's task is not this one's: a switch closes it and clears the filters,
  // during render, so the open panel never reads the old task under the new namespace.
  const [openFor, setOpenFor] = useState(caller);
  if (openFor !== caller) {
    setOpenFor(caller);
    setOpenRow(null);
    setSelectedId(null);
    setFilters(NO_FILTERS);
    // The old inbox is read again only once the new namespace says it has one; its rows
    // never draw under the new namespace's inbox.
    setPicked(null);
    setRows(null);
    setRefreshError(null);
  }
  // The caller as of the latest render, for an answer that lands after a switch.
  const callerNow = useRef(caller);
  callerNow.current = caller;

  useEffect(() => {
    // A namespace switch restores only its own inboxes, and drops any row read in flight.
    // `inboxReads` is the error state's Retry.
    void inboxReads;
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
        setInboxError(e);
        setInboxes([]);
      });
    refreshCounts();
    return () => {
      live = false;
    };
  }, [caller, refreshCounts, inboxReads]);

  // A navigation to Tasks while it is up (the bell's inbox) names the inbox in the URL:
  // open it, when this namespace has it.
  const navSeq = useStore((s) => s.navSeq);
  useEffect(() => {
    if (!inboxes || navSeq === 0) return;
    const want = readQuery("inbox");
    if (want && (want === ALL_INBOXES || inboxes.some((i) => i.inbox === want))) setPicked(want);
  }, [navSeq, inboxes]);

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
        setRefreshError(null);
      })
      .catch((e) => {
        if (seq !== rowsSeq.current) return;
        if (rowsNow.current !== null) {
          setRefreshError(e);
          return;
        }
        setRowsError(e);
        setRows([]);
      });
  }, [caller, picked, inboxes]);

  useEffect(() => {
    if (!picked) return;
    setRows(null);
    setRowsError(null);
    setRefreshError(null);
    setViewParam("inbox", picked);
    refresh();
  }, [picked, refresh]);
  useVisiblePoll(refresh, 60_000, !!picked);
  // Counts a failed read left unknown are read again on the same beat until they land.
  useVisiblePoll(refreshCounts, 60_000, counts === null);

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

  const loaded = rows !== null && !rowsError;
  const attention = useMemo(() => (loaded ? attentionCounts(rows ?? [], now) : null), [loaded, rows, now]);
  const kinds = useMemo(() => kindsOf(rows ?? []), [rows]);
  // The lanes filter a render behind the toolbar, so a keystroke in the search paints the
  // box before the memoised lanes render again.
  const listFilters = useDeferredValue(filters);
  const visible = useMemo(
    () => (rows ?? []).filter((r) => passesFilters(r, listFilters, ownerAgentOf, now)),
    [rows, listFilters, ownerAgentOf, now],
  );
  const groups = useMemo(() => groupTasks(visible), [visible]);
  const filtering = !!(listFilters.attention || listFilters.kind || listFilters.poster || listFilters.q.trim());

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
    setViewParam("task", null);
  };
  const toggleTile = (k: Attention) => setFilters((f) => ({ ...f, attention: f.attention === k ? null : k }));
  const toggleLane = useCallback((g: Group) => setCollapsed((c) => ({ ...c, [g]: !c[g] })), []);

  const openTask = useCallback((r: TaskListRow) => {
    setSelectedId(r.task_id);
    setOpenRow(r);
    setReloadKey(0);
    setViewParam("task", r.task_id);
  }, []);
  const closeTask = useCallback(() => {
    // Closing the side panel from inside it hands the focus back to its row (the sheet's
    // dialog does the same on its own close).
    const open = openRowRef.current;
    const from = document.activeElement;
    if (open && from instanceof Element && from.closest(".tq-panel"))
      document.querySelector<HTMLElement>(`[data-task-id="${open.task_id}"]`)?.focus({ preventScroll: true });
    setOpenRow(null);
    setViewParam("task", null);
  }, []);

  // Moving the selection (j / k here, the arrow keys in the list) follows with the open
  // panel and takes the focus to the row.
  const selectRow = useCallback(
    (r: TaskListRow) => {
      setSelectedId(r.task_id);
      if (openRowRef.current) openTask(r);
      const el = document.querySelector<HTMLElement>(`[data-task-id="${r.task_id}"]`);
      el?.scrollIntoView?.({ block: "nearest" });
      el?.focus({ preventScroll: true });
    },
    [openTask],
  );

  // Where the focus goes once a move went through: on to the next row after a move made
  // from the list (the row itself is leaving its lane), to the panel's title after one
  // made in the panel (the button pressed may now be another move).
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const focusAfter = useCallback(
    (origin: Origin, next: string | null) => {
      if (origin === "panel") {
        document.querySelector<HTMLElement>(".tq-panel [data-panel-title]")?.focus({ preventScroll: true });
        return;
      }
      const r = next ? rowsRef.current?.find((x) => x.task_id === next) : undefined;
      if (r) selectRow(r);
    },
    [selectRow],
  );

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
    async (action: TaskAction, task: TaskListRow | TaskRow, origin: Origin) => {
      if (!mayWrite || !actionsFor(task.status).includes(action)) return;
      if (action === "edit") {
        if (isFullTask(task)) setEditing(task);
        return;
      }
      const next = origin === "list" ? neighbourOf(task.task_id) : null;
      // Resume and start never open the dialog; ruling them out narrows the action to a ReasonAction.
      if (action !== "resume" && action !== "start" && DIALOG_ACTIONS.has(action)) {
        dialogDone.current = false;
        setDialog({ action, task, origin, next });
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
        toast(actionDone(action, task.title), "ok");
        // A switch while the move was out: the list on screen is another namespace's.
        if (callerNow.current !== caller) return;
        afterChange(r.task);
        focusAfter(origin, next);
      } catch (e) {
        toastError(ACTION_LABEL[action], e);
      }
    },
    [mayWrite, caller, afterChange, focusAfter],
  );

  // The action dialog closes: on to the next row once the move is made from the list, back
  // to the row when it is not; a move made in the panel ends on the panel's title.
  const dialogCloseFocus = (e: Event) => {
    const d = dialog;
    if (!d) return;
    if (d.origin === "panel") {
      if (!dialogDone.current) return; // back to the button that opened it
      e.preventDefault();
      focusAfter("panel", null);
      return;
    }
    e.preventDefault();
    if (dialogDone.current) focusAfter("list", d.next);
    else document.querySelector<HTMLElement>(`[data-task-id="${d.task.task_id}"]`)?.focus({ preventScroll: true });
  };

  const onHistory = useCallback((taskId: string, events: TaskEvent[]) => {
    const note = latestProgressNote(events);
    setProgressNotes((m) => (m.get(taskId) === note ? m : new Map(m).set(taskId, note)));
  }, []);

  // Keys: j / k over the rows on screen, Enter opens, a / s / c / x / d act on the
  // selected row, / focuses the search, Esc closes the panel. Ignored while typing or
  // while a dialog or a menu is open.
  const rowsById = useMemo(() => new Map((rows ?? []).map((r) => [r.task_id, r])), [rows]);
  const stateRef = useRef({ selectedId, openRow, onAction, openTask, closeTask, selectRow, rowsById });
  stateRef.current = { selectedId, openRow, onAction, openTask, closeTask, selectRow, rowsById };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || inEditable(e.target) || shortcutOff(e)) return;
      if (document.querySelector('[role="dialog"], [role="menu"]')) return;
      const { selectedId: sel, openRow: open, onAction: act, openTask: openT, closeTask: closeT } = stateRef.current;
      const list = shownTaskIds()
        .map((id) => stateRef.current.rowsById.get(id))
        .filter((r): r is TaskListRow => !!r);
      // A key pressed on a row or card acts on that one, and j / k move from it (focus and
      // selection can part: a card Tabbed to, the row Show all focused).
      const focusedId =
        e.target instanceof Element ? e.target.closest<HTMLElement>("[data-task-id]")?.dataset.taskId : undefined;
      const focused = focusedId ? stateRef.current.rowsById.get(focusedId) : undefined;
      const i = list.findIndex((r) => r.task_id === (focused?.task_id ?? sel));
      const move = (to: number) => {
        const r = list[Math.max(0, Math.min(list.length - 1, to))];
        if (r) stateRef.current.selectRow(r);
      };
      // Else the selected row as drawn, else the task open in the panel (a deep link past a
      // lane's first rows); a selection a filter hid, with no panel open, takes no keys.
      const row =
        focused ??
        (i >= 0 ? list[i] : open && open.task_id === sel ? (stateRef.current.rowsById.get(sel) ?? open) : null);
      const origin: Origin = e.target instanceof Element && e.target.closest(".tq-panel") ? "panel" : "list";
      const keyed: Record<string, TaskAction> = { a: "accept", s: "start", c: "complete", x: "wont_do", d: "defer" };
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
        case "/":
          e.preventDefault();
          document.querySelector<HTMLElement>('.tq-main input[aria-label="Search tasks"]')?.focus();
          break;
        default:
          if (row && keyed[e.key]) {
            e.preventDefault();
            void act(keyed[e.key], row, origin);
          }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const menuFor = useCallback(
    (r: TaskListRow): RowMenuItem[] => {
      const offered = mayWrite ? actionsFor(r.status) : [];
      const items: RowMenuItem[] = ROW_MENU.filter((a) => offered.includes(a)).map((a) => ({
        label: menuLabel(a),
        onSelect: () => void onAction(a, r, "list"),
        danger: a === "cancel",
      }));
      items.push({ label: "Copy id", onSelect: () => copyTaskId(r.task_id) });
      return items;
    },
    [mayWrite, onAction],
  );

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
        void onAction("accept", r, "list");
      },
      menuFor,
      onDrop: (r: TaskListRow, to: Group) => {
        const a = dropAction(r.status, to);
        if (a) void onAction(a, r, "list");
      },
    }),
    [now, ownerAgentOf, leaseSecondsOf, progressNotes, selectedId, mayWrite, openTask, selectRow, onAction, menuFor],
  );

  const createInbox = inbox?.inbox ?? inboxes?.[0]?.inbox ?? null;
  const newTask = mayWrite && createInbox ? () => setCreating(true) : null;
  // The empty inbox's own New task is the page's one primary while it shows.
  const emptyInbox = loaded && rows?.length === 0;
  const openCount = visible.length - groups.closed.length;
  // A narrow page always draws the list, whatever mode is saved.
  const listed = mode === "list" || narrow;
  const onlyClosed = listed && !filtering && openCount === 0 && visible.length > 0 && collapsed.closed;
  // An inbox reads its first 200 tasks: when one held more, the count and a search say they
  // cover those, not every task the inbox holds.
  const cut = truncated ? " in the first 200 per inbox" : "";
  const count =
    loaded && !emptyInbox
      ? filtering
        ? `${visible.length} ${visible.length === 1 ? "match" : "matches"}${cut}`
        : `${visible.length} ${visible.length === 1 ? "task" : "tasks"}${cut}`
      : undefined;
  const clearFilters = () => setFilters(NO_FILTERS);

  // The open task beside the list, or (below xl) as the content of a modal sheet.
  const panelFor = (task: TaskListRow, sheet: boolean) => (
    <TaskPanel
      identity={caller}
      row={task}
      inboxes={inboxes ?? []}
      canWrite={mayWrite}
      sheet={sheet}
      now={now}
      reloadKey={reloadKey}
      onClose={closeTask}
      onAction={(a, t) => {
        void onAction(a, t, "panel");
      }}
      onHistory={onHistory}
      onOpenTask={(id) => {
        const r = rows?.find((x) => x.task_id === id);
        if (r) openTask(r);
      }}
    />
  );

  // A narrow page, or the list beside the open panel: the toolbar holds the search, the filters a row under it.
  const compact = narrow || (!!openRow && !sheetLayout);
  const filterSelects = (
    <>
      {(kinds.length > 1 || filters.kind) && (
        <Select
          value={filters.kind ?? ANY}
          onSelect={(v) => setFilters((f) => ({ ...f, kind: v === ANY ? null : v }))}
          triggerProps={{ "aria-label": "Kind" }}
          className={compact ? "min-w-0 flex-1" : "w-40 shrink-0"}
        >
          <Select.Item value={ANY}>Every kind</Select.Item>
          {kinds.map((k) => (
            <Select.Item key={k} value={k}>
              {k}
            </Select.Item>
          ))}
        </Select>
      )}
      {(!!rows?.length || filters.poster) && (
        <Select
          value={filters.poster ?? ANY}
          onSelect={(v) => setFilters((f) => ({ ...f, poster: v === "me" || v === "others" ? v : null }))}
          triggerProps={{ "aria-label": "Posted by" }}
          className={compact ? "min-w-0 flex-1" : "w-40 shrink-0"}
        >
          {POSTED_BY.map(([v, label]) => (
            <Select.Item key={v} value={v}>
              {label}
            </Select.Item>
          ))}
        </Select>
      )}
    </>
  );

  return (
    <PageContainer>
      <PageHeader
        title="Tasks"
        actions={
          <>
            {/* A narrow page shows the list: a board's columns do not fit it. */}
            {!narrow && (
              <ButtonGroup
                aria-label="View"
                selected={mode}
                onClick={(v) => setModeAndRemember(v === "board" ? "board" : "list")}
                options={[
                  { value: "list", label: <ViewLabel icon="list-bulleted" text="List" /> },
                  { value: "board", label: <ViewLabel icon="services" text="Board" /> },
                ]}
              />
            )}
            {/* The page's rarer controls; the keys are in the help dialog and each action's tooltip. */}
            <Dropdown modal={false}>
              <MenuTrigger>
                <IconButton icon="dots-horizontal" label="Page options" />
              </MenuTrigger>
              <Dropdown.Content align="end" aria-label="Page options" aria-labelledby={undefined}>
                {mayAdmin && inbox && (
                  <Dropdown.Item onSelect={() => setSettings("edit")}>Inbox settings…</Dropdown.Item>
                )}
                {mayAdmin && <Dropdown.Item onSelect={() => setSettings("register")}>Register inbox…</Dropdown.Item>}
                <Dropdown.Item onSelect={() => openHelp(true)}>Keyboard shortcuts</Dropdown.Item>
              </Dropdown.Content>
            </Dropdown>
            {newTask && !emptyInbox && (
              <Button type="primary" htmlType="button" iconLeft="plus" onClick={newTask}>
                New task
              </Button>
            )}
          </>
        }
      >
        {/* A narrow page's top bar names the namespace; the title row keeps its actions beside it. */}
        <span className="hidden sm:contents">
          <ScopeBadge kind="namespace" value={identity.namespace} />
        </span>
      </PageHeader>

      {inboxes === null && (
        <LoadingSkeleton label="Loading the inboxes">
          <div className="space-y-3">
            <SkeletonCard />
            <SkeletonCard />
          </div>
        </LoadingSkeleton>
      )}
      {inboxes && inboxError !== null && (
        <ListState kind="error" things="inboxes" error={inboxError} onRetry={() => setInboxReads((n) => n + 1)} />
      )}
      {inboxes && inboxError === null && inboxes.length === 0 && (
        <EmptyState
          icon="email"
          title="No inbox here yet"
          description={`An inbox is one agent's queue, owned by ${identity.namespace}. An admin of this namespace registers it; writers then pull from it and the trust table says who else may post.`}
          action={mayAdmin ? { label: "Register inbox", onClick: () => setSettings("register") } : undefined}
          details={
            <>
              Registering an inbox names its owner agent, the agents allowed to post into it without reading it and the
              disclosure levels it accepts. Agents then take its tasks one at a time, each on a lease; people work the
              board here.
            </>
          }
        />
      )}

      {inboxes && inboxes.length > 0 && picked && (
        <>
          {narrow && (
            // The tiles and the filters are one menu on the inbox's row, so the tasks start
            // within the chrome budget.
            <div className="mb-3 flex items-center gap-2">
              <div className="min-w-0 flex-1">
                <InboxSelect
                  inboxes={inboxes}
                  counts={counts ? railCounts(counts) : null}
                  picked={picked}
                  onPick={pick}
                />
              </div>
              <FiltersMenu
                filters={filters}
                counts={attention}
                kinds={kinds}
                onChange={(next) => setFilters((f) => ({ ...f, ...next }))}
              />
            </div>
          )}
          {!narrow && <AttentionTiles counts={attention} active={filters.attention} onToggle={toggleTile} />}
          <div className="tq-body" data-panel={!!openRow && !sheetLayout}>
            {!narrow && (
              <InboxRail inboxes={inboxes} counts={counts ? railCounts(counts) : null} picked={picked} onPick={pick} />
            )}
            <div className="tq-main min-w-0">
              <ListToolbar label="Search and filters" count={count}>
                <ListToolbarSearch>
                  <SearchField
                    value={filters.q}
                    onChange={(q) => setFilters((f) => ({ ...f, q }))}
                    placeholder="Search tasks"
                    aria-label="Search tasks"
                  />
                </ListToolbarSearch>
                {!compact && filterSelects}
              </ListToolbar>
              {compact && !narrow && <div className="-mt-1 mb-3 flex items-center gap-2">{filterSelects}</div>}

              {rows === null && (
                <LoadingSkeleton label="Loading tasks">
                  <div className="space-y-3">
                    <SkeletonCard />
                    <SkeletonCard />
                    <SkeletonCard />
                  </div>
                </LoadingSkeleton>
              )}
              {rows !== null && rowsError !== null && (
                <ListState kind="error" things="tasks" error={rowsError} onRetry={refresh} />
              )}
              {refreshError !== null && rowsError === null && (
                <div className="mb-2 text-xs text-text-muted" role="status">
                  {`Showing the last read; couldn't refresh the tasks: ${humanizeError(refreshError, { bare: true, read: true })}`}
                </div>
              )}
              {emptyInbox && (
                <ListState
                  kind="empty"
                  icon="email"
                  title={inbox ? `No tasks in ${inbox.inbox} yet` : "No tasks in these inboxes yet"}
                  description={
                    newTask ? "Agents post tasks here; people use New task." : "Agents and people post tasks here."
                  }
                  action={newTask ? { label: "New task", onClick: newTask } : undefined}
                />
              )}
              {loaded && !emptyInbox && visible.length === 0 && (
                <ListState
                  kind="filtered"
                  things="tasks"
                  filter={filterSummary(filters)}
                  among={truncated ? "the first 200 per inbox" : undefined}
                  onClear={clearFilters}
                />
              )}
              {onlyClosed && (
                <ListState
                  kind="empty"
                  icon="check-in-circle"
                  title="No open tasks"
                  description={`${visible.length} closed ${visible.length === 1 ? "task" : "tasks"} in this inbox.`}
                  action={{ label: "Show closed", onClick: () => toggleLane("closed") }}
                />
              )}
              {loaded &&
                visible.length > 0 &&
                !onlyClosed &&
                (listed ? (
                  <TaskList
                    groups={groups}
                    ctx={ctx}
                    collapsed={collapsed}
                    onToggle={toggleLane}
                    forceOpen={filtering}
                  />
                ) : (
                  <TaskBoard groups={groups} ctx={ctx} filtering={filtering} />
                ))}
            </div>
            {openRow && !sheetLayout && panelFor(openRow, false)}
          </div>
        </>
      )}

      {sheetLayout && (
        <RadixDialog.Root
          open={!!openRow}
          onOpenChange={(v) => {
            if (!v) closeTask();
          }}
        >
          <RadixDialog.Portal>
            <RadixDialog.Overlay className="fixed inset-0 z-40 bg-overlay" />
            {openRow && panelFor(openRow, true)}
          </RadixDialog.Portal>
        </RadixDialog.Root>
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
            onDone={(t, replacement) => {
              dialogDone.current = true;
              afterChange(t, replacement);
            }}
            onCloseAutoFocus={dialogCloseFocus}
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
