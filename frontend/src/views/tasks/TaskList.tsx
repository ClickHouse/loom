import {
  memo,
  useLayoutEffect,
  useRef,
  useState,
  type FocusEvent,
  type HTMLAttributes,
  type KeyboardEvent,
} from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, GROUP_META, assigneeOf, sublineOf, type Group, type Subline } from "../../lib/tasks";
import { Assignee, KindTag, LaneBadge, LeaseBar, PriorityBar } from "./TaskBits";
import { useTaskDrag } from "./drag";
import { absoluteTime } from "../../lib/format";
import { Unavailable } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";
import { RowMenu, type RowMenuItem } from "../../components/ui/RowMenu";

/**
 * The list: five collapsible groups by what happens next — Triage, To do, In progress,
 * Waiting, Closed — each with its state badge, a count and a hint. A row reads in two
 * lines: the priority and the title, with the assignee, the age and the row's menu on the
 * right; then the kind and the one line that matters for its state (who posted it, why it
 * waits, the lease). A triage row also carries its one inline action, Accept; every other
 * move is in the menu. A group shows 20 rows, then "Show all N".
 *
 * The DOM is a grid (the WAI-ARIA grid pattern): each group is a rowgroup whose first row
 * holds the collapse button, then its task rows. The grid is one tab stop: Up / Down move
 * between group headers and task rows, Home / End jump to the ends, and Left /
 * Right walk a row's controls (a header is one cell, so there they do nothing). A writer
 * drags a row onto another group to move it (`useTaskDrag`); while a row is held, empty
 * and hidden groups show as drop targets.
 */
export interface RowContext {
  now: number;
  ownerAgentOf: (inbox: string) => string | undefined;
  leaseSecondsOf: (inbox: string) => number;
  /** The latest progress note per task, from the histories the page has read. */
  progressNotes: ReadonlyMap<string, string>;
  selectedId: string | null;
  onOpen: (r: TaskListRow) => void;
  /** Move the selection (and the focus) to a row, as Up / Down do. */
  onSelect: (r: TaskListRow) => void;
  /** Why the caller's role cannot move a task, or null: the moves stay in view with it. */
  writeReason: string | null;
  onAccept: (r: TaskListRow) => void;
  /** The row's menu: every move its status allows but the inline one. */
  menuFor: (r: TaskListRow) => RowMenuItem[];
  /** A card dropped on a group: the move `dropAction` names, if any. */
  onDrop: (r: TaskListRow, to: Group) => void;
}

/** A lane lists this many rows before "Show all N": Tasks has no pager. */
export const LANE_ROWS = 20;

/** A row's second line. `plain` writes an outcome's URL as text, for a card that is itself a button. */
export function SublineView({ r, ctx, plain = false }: { r: TaskListRow; ctx: RowContext; plain?: boolean }) {
  const s: Subline | null = sublineOf(r, ctx.now, ctx.progressNotes.get(r.task_id));
  return (
    <span className="tq-sub">
      <KindTag kind={r.kind} />
      {r.status === "in_progress" && (
        <LeaseBar leaseUntil={r.lease_until} leaseSeconds={ctx.leaseSecondsOf(r.inbox)} now={ctx.now} />
      )}
      {s && (s.italic ? <span className="reason">{s.text}</span> : <span className="truncate">{s.text}</span>)}
      {s?.href && plain && (
        <span className="truncate">{s.href.replace(/^https?:\/\/(www\.)?(github\.com\/)?/, "")}</span>
      )}
      {s?.href && !plain && (
        <a
          href={s.href}
          target="_blank"
          rel="noreferrer"
          className="truncate"
          tabIndex={-1}
          onClick={(e) => e.stopPropagation()}
        >
          {s.href.replace(/^https?:\/\/(www\.)?(github\.com\/)?/, "")}
        </a>
      )}
      {s?.suffix && <span className="whitespace-nowrap">{s.suffix}</span>}
    </span>
  );
}

function Meta({ r, ctx }: { r: TaskListRow; ctx: RowContext }) {
  const who = assigneeOf(r, ctx.ownerAgentOf(r.inbox));
  return (
    <span className="tq-meta">
      {r.status === "triage" && (
        <Unavailable reason={ctx.writeReason}>
          <Button
            type="secondary"
            htmlType="button"
            onClick={(e) => {
              e.stopPropagation();
              ctx.onAccept(r);
            }}
            aria-label={`Accept ${r.title}`}
          >
            Accept
          </Button>
        </Unavailable>
      )}
      {who && <Assignee name={who.name} label={who.label} />}
      <ListTime time={r.created_at} quiet />
      <RowMenu name={r.title} items={ctx.menuFor(r)} />
    </span>
  );
}

// biome-ignore-start lint/a11y/useSemanticElements: a div grid — rows are draggable and grouped into collapsible groups, which a <table> cannot hold
// biome-ignore-start lint/a11y/useFocusableInteractive: each task row is a tab stop; cells, header and placeholder rows are read, not visited
function TaskRowView({
  r,
  ctx,
  drag,
  tabStop,
}: {
  r: TaskListRow;
  ctx: RowContext;
  drag: HTMLAttributes<HTMLElement>;
  tabStop: boolean;
}) {
  const selected = ctx.selectedId === r.task_id;
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" && e.target === e.currentTarget) {
      e.preventDefault();
      ctx.onOpen(r);
    }
  };
  return (
    <div
      role="row"
      tabIndex={tabStop ? 0 : -1}
      aria-selected={selected}
      aria-label={r.title}
      aria-describedby={`${r.task_id}-posted`}
      data-task-id={r.task_id}
      data-nav={`task:${r.task_id}`}
      className="tq-row"
      onClick={(e) => {
        // A control in the row, or its menu (whose clicks reach the row through the portal), is its own.
        if (e.target instanceof Element && e.target.closest('button, a[href], [role="menu"]')) return;
        ctx.onOpen(r);
      }}
      onKeyDown={onKey}
      {...drag}
    >
      <span className="tq-pri" role="gridcell">
        <PriorityBar priority={r.priority} />
      </span>
      <span className="tq-title" role="gridcell">
        <span className="title text-sm">{r.title}</span>
      </span>
      <span className="tq-metacell" role="gridcell">
        <Meta r={r} ctx={ctx} />
        {/* The row shows a relative time; the full one is its description (and the panel's). */}
        <span id={`${r.task_id}-posted`} className="sr-only">
          Posted {absoluteTime(r.created_at)}
        </span>
      </span>
      <span className="tq-subcell" role="gridcell">
        <SublineView r={r} ctx={ctx} />
      </span>
    </div>
  );
}

interface ListProps {
  groups: Record<Group, TaskListRow[]>;
  ctx: RowContext;
  /** Folded groups (the page's, so an empty state can open Closed). */
  collapsed: Record<Group, boolean>;
  onToggle: (g: Group) => void;
  /** With a filter on, every group opens (the person asked to see these rows). */
  forceOpen: boolean;
}

// What an open, empty group says. Only Triage and Closed are listed while empty; the
// others show only as drop targets while a row is held.
const EMPTY_LANE: Record<Group, string> = {
  triage: "Nothing waiting for a decision.",
  todo: "Drop here.",
  in_progress: "Drop here.",
  waiting: "Drop here.",
  closed: "Nothing closed yet.",
};

/** What an empty group says under a filter: the group may hold tasks the filter hides. */
export const NO_MATCH = "None match the filters.";

/** The controls inside a row (its buttons, its link, its time) leave the tab order with
 *  it: the grid is one tab stop, and Left / Right reach them. */
const IN_ROW = '[role="row"][data-task-id] :is(a[href], button, [tabindex="0"])';

// Memoised: a keystroke in the toolbar's search renders the page, not every row of every lane.
export const TaskList = memo(function TaskList({ groups, ctx, collapsed, onToggle, forceOpen }: ListProps) {
  const [active, setActive] = useState<string | null>(null);
  const [all, setAll] = useState<ReadonlySet<Group>>(new Set());
  const drag = useTaskDrag(ctx.writeReason === null, ctx.onDrop);
  const grid = useRef<HTMLDivElement>(null);
  // After every render: the controls React just drew inside rows take no tab stop.
  useLayoutEffect(() => {
    for (const el of grid.current?.querySelectorAll<HTMLElement>(IN_ROW) ?? []) el.tabIndex = -1;
  });

  // A held row needs every group it could land in, even an empty one.
  const shown = GROUPS.filter((g) => drag.holding || groups[g].length > 0 || g === "triage" || g === "closed");
  const isCollapsed = (g: Group) => collapsed[g] && !forceOpen;
  const listed = (g: Group) => (all.has(g) ? groups[g] : groups[g].slice(0, LANE_ROWS));
  const byKey = new Map<string, TaskListRow>();
  const keys: string[] = [];
  for (const g of shown) {
    keys.push(`group:${g}`);
    if (isCollapsed(g)) continue;
    for (const r of listed(g)) {
      keys.push(`task:${r.task_id}`);
      byKey.set(`task:${r.task_id}`, r);
    }
    if (listed(g).length < groups[g].length) keys.push(`more:${g}`);
  }
  // The one tab stop: the last focused item, else the selected row, else the first header.
  const selectedKey = ctx.selectedId ? `task:${ctx.selectedId}` : null;
  const tabStop =
    active && keys.includes(active) ? active : selectedKey && keys.includes(selectedKey) ? selectedKey : keys[0];

  // "Show all N" opens the rest in place and takes the reader to the first row it added. It
  // moves focus only: selecting the row would also open it in place of the task in the panel.
  const showAll = (g: Group) => {
    const first = groups[g][LANE_ROWS];
    setAll((s) => new Set(s).add(g));
    if (!first) return;
    const key = `task:${first.task_id}`;
    setActive(key);
    window.setTimeout(() => document.querySelector<HTMLElement>(`[data-nav="${CSS.escape(key)}"]`)?.focus(), 0);
  };

  const onFocus = (e: FocusEvent<HTMLDivElement>) => {
    const key = (e.target as HTMLElement).closest<HTMLElement>("[data-nav]")?.dataset.nav;
    if (key) setActive(key);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const target = e.target as HTMLElement;
    // A menu open from a row keeps its own arrow keys.
    if (target.closest('[role="menu"]')) return;
    const here = target.closest<HTMLElement>("[data-nav]");
    if (!here) return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (here.getAttribute("role") !== "row") return;
      const stops = [here, ...here.querySelectorAll<HTMLElement>("a[href], button:not(:disabled)")];
      const next = stops[stops.indexOf(target) + (e.key === "ArrowRight" ? 1 : -1)];
      if (!next) return;
      e.preventDefault();
      next.focus();
      return;
    }
    const items = [...e.currentTarget.querySelectorAll<HTMLElement>("[data-nav]")];
    const i = items.indexOf(here);
    const to =
      e.key === "ArrowDown"
        ? i + 1
        : e.key === "ArrowUp"
          ? i - 1
          : e.key === "Home"
            ? 0
            : e.key === "End"
              ? items.length - 1
              : null;
    if (to === null) return;
    e.preventDefault();
    const el = items[to];
    if (!el) return;
    const row = byKey.get(el.dataset.nav ?? "");
    if (row) ctx.onSelect(row);
    else el.focus();
  };

  return (
    <div
      ref={grid}
      role="grid"
      aria-label="Tasks by what happens next"
      className="min-w-0"
      onFocus={onFocus}
      onKeyDown={onKeyDown}
    >
      {shown.map((g) => {
        const rows = groups[g];
        const meta = GROUP_META[g];
        const closed = isCollapsed(g);
        const rest = rows.length - listed(g).length;
        return (
          <div
            key={g}
            role="rowgroup"
            className="tq-group"
            data-collapsed={closed}
            aria-labelledby={`tq-group-${g}`}
            {...drag.target(g)}
          >
            <div role="row">
              <div role="gridcell">
                <h2 className="m-0 text-sm font-normal">
                  <button
                    type="button"
                    className="tq-ghead"
                    tabIndex={tabStop === `group:${g}` ? 0 : -1}
                    data-nav={`group:${g}`}
                    aria-expanded={!closed}
                    aria-controls={closed ? undefined : `tq-rows-${g}`}
                    onClick={() => onToggle(g)}
                  >
                    <LaneBadge group={g} id={`tq-group-${g}`} />
                    <span className="text-xs text-text-muted tabular">{rows.length}</span>
                    <span className="ml-auto text-xs text-text-muted hidden sm:inline">{meta.hint}</span>
                    <Icon name="chevron-down" size="xs" className="car" aria-hidden="true" />
                  </button>
                </h2>
              </div>
            </div>
            {!closed && (
              <div id={`tq-rows-${g}`} className="tq-rows">
                {rows.length === 0 ? (
                  <div role="row">
                    <div role="gridcell" className="px-3.5 py-3.5 text-sm text-text-muted">
                      {forceOpen && (g === "triage" || g === "closed") ? NO_MATCH : EMPTY_LANE[g]}
                    </div>
                  </div>
                ) : (
                  listed(g).map((r) => (
                    <TaskRowView
                      key={r.task_id}
                      r={r}
                      ctx={ctx}
                      drag={drag.source(r)}
                      tabStop={tabStop === `task:${r.task_id}`}
                    />
                  ))
                )}
                {rest > 0 && (
                  <div role="row">
                    <div role="gridcell" className="tq-more">
                      <Button
                        type="empty"
                        htmlType="button"
                        tabIndex={tabStop === `more:${g}` ? 0 : -1}
                        data-nav={`more:${g}`}
                        onClick={() => showAll(g)}
                      >
                        {`Show all ${rows.length}`}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
});
// biome-ignore-end lint/a11y/useFocusableInteractive: the grid ends
// biome-ignore-end lint/a11y/useSemanticElements: the grid ends
