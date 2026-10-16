import { useState, type FocusEvent, type HTMLAttributes, type KeyboardEvent, type MouseEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, GROUP_META, posterName, rel, sublineOf, type Group, type Subline } from "../../lib/tasks";
import { Avatar, KindTag, LeaseBar, PosterAvatar, PriorityBar } from "./TaskBits";
import { useTaskDrag } from "./drag";
import { absoluteTime } from "../../lib/format";

/**
 * The list: five collapsible groups by what happens next — Triage, To do, In progress,
 * Waiting, Closed — each with a swatch, a count and a hint. A row carries the decision:
 * the priority bar, the kind, the title, the one line that matters for its state, and a
 * meta cell — inline Accept / Won't do in Triage, the owner and lease bar in progress,
 * the poster and age otherwise. The DOM is a grid: each group is a rowgroup whose first
 * row holds the collapse button, then its task rows. The grid is one tab stop: Up / Down
 * (and j / k) move between group headers and task rows, Home / End jump to the ends, and
 * Left / Right walk a row's controls (a header is one cell, so there they do nothing). A
 * writer drags a row onto another group to move it (`useTaskDrag`); while a row is held,
 * empty and hidden groups show as drop targets.
 */
export interface RowContext {
  now: number;
  ownerAgentOf: (inbox: string) => string | undefined;
  leaseSecondsOf: (inbox: string) => number;
  /** The latest progress note per task, from the histories the page has read. */
  progressNotes: ReadonlyMap<string, string>;
  selectedId: string | null;
  onOpen: (r: TaskListRow) => void;
  /** Move the selection (and the focus) to a row, as j / k do. */
  onSelect: (r: TaskListRow) => void;
  canWrite: boolean;
  onAccept: (r: TaskListRow) => void;
  onWontDo: (r: TaskListRow) => void;
  /** A card dropped on a group: the move `dropAction` names, if any. */
  onDrop: (r: TaskListRow, to: Group) => void;
}

function SublineView({ s }: { s: Subline | null }) {
  if (!s) return null;
  return (
    <span className="tq-sub">
      {s.italic ? <span className="reason">{s.text}</span> : <span>{s.text}</span>}
      {s.href && (
        <a href={s.href} target="_blank" rel="noreferrer" tabIndex={-1} onClick={(e) => e.stopPropagation()}>
          {s.href.replace(/^https?:\/\/(www\.)?(github\.com\/)?/, "")}
        </a>
      )}
      {s.suffix && <span>{s.suffix}</span>}
    </span>
  );
}

function Meta({ r, ctx }: { r: TaskListRow; ctx: RowContext }) {
  if (r.status === "in_progress") {
    return (
      <span className="tq-meta">
        <span className="tq-who">
          <Avatar name={r.owner || "?"} kind="me" />
          {r.owner || "—"}
        </span>
        <LeaseBar leaseUntil={r.lease_until} leaseSeconds={ctx.leaseSecondsOf(r.inbox)} now={ctx.now} />
      </span>
    );
  }
  if (r.status === "triage") {
    const stop = (e: MouseEvent) => e.stopPropagation();
    return (
      <span className="tq-meta">
        {ctx.canWrite && (
          <span className="tq-acts">
            <Button
              type="secondary"
              htmlType="button"
              tabIndex={-1}
              onClick={(e) => {
                stop(e);
                ctx.onAccept(r);
              }}
              aria-label={`Accept ${r.title}`}
            >
              Accept
            </Button>
            <Button
              type="empty"
              htmlType="button"
              tabIndex={-1}
              onClick={(e) => {
                stop(e);
                ctx.onWontDo(r);
              }}
              aria-label={`Won't do ${r.title}`}
            >
              Won't do…
            </Button>
          </span>
        )}
        <time className="tabular" dateTime={r.created_at}>
          {rel(r.created_at, ctx.now)}
        </time>
      </span>
    );
  }
  return (
    <span className="tq-meta">
      <span className="tq-who">
        <PosterAvatar r={r} ownerAgent={ctx.ownerAgentOf(r.inbox)} />
        <span className="truncate max-w-40">{posterName(r)}</span>
      </span>
      <time className="tabular" dateTime={r.created_at}>
        {rel(r.created_at, ctx.now)}
      </time>
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
      onClick={() => ctx.onOpen(r)}
      onKeyDown={onKey}
      {...drag}
    >
      <span className="grid" role="gridcell">
        <PriorityBar priority={r.priority} />
      </span>
      <span className="min-w-0" role="gridcell">
        <span className="t">
          <KindTag kind={r.kind} />
          <span className="title text-sm">{r.title}</span>
        </span>
        <SublineView s={sublineOf(r, ctx.now, ctx.progressNotes.get(r.task_id))} />
      </span>
      <span role="gridcell">
        <Meta r={r} ctx={ctx} />
        {/* The row shows a relative time; the full one is its description (and the panel's). */}
        <span id={`${r.task_id}-posted`} className="sr-only">
          Posted {absoluteTime(r.created_at)}
        </span>
      </span>
    </div>
  );
}

interface ListProps {
  groups: Record<Group, TaskListRow[]>;
  ctx: RowContext;
  /** With a filter on, every group opens (the person asked to see these rows). */
  forceOpen: boolean;
  showClosed: boolean;
}

export function TaskList({ groups, ctx, forceOpen, showClosed }: ListProps) {
  const [collapsed, setCollapsed] = useState<Record<Group, boolean>>({
    triage: false,
    todo: false,
    in_progress: false,
    waiting: false,
    closed: true,
  });
  const [active, setActive] = useState<string | null>(null);
  const drag = useTaskDrag(ctx.canWrite, ctx.onDrop);

  // A held row needs every group it could land in, even an empty or hidden one.
  const shown = GROUPS.filter(
    (g) => drag.holding || ((groups[g].length > 0 || g === "triage") && (g !== "closed" || showClosed)),
  );
  const isCollapsed = (g: Group) => collapsed[g] && !forceOpen;
  const byKey = new Map<string, TaskListRow>();
  const keys: string[] = [];
  for (const g of shown) {
    keys.push(`group:${g}`);
    if (isCollapsed(g)) continue;
    for (const r of groups[g]) {
      keys.push(`task:${r.task_id}`);
      byKey.set(`task:${r.task_id}`, r);
    }
  }
  // The one tab stop: the last focused item, else the selected row, else the first header.
  const selectedKey = ctx.selectedId ? `task:${ctx.selectedId}` : null;
  const tabStop =
    active && keys.includes(active) ? active : selectedKey && keys.includes(selectedKey) ? selectedKey : keys[0];

  const onFocus = (e: FocusEvent<HTMLDivElement>) => {
    const key = (e.target as HTMLElement).closest<HTMLElement>("[data-nav]")?.dataset.nav;
    if (key) setActive(key);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const target = e.target as HTMLElement;
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
                    onClick={() => setCollapsed((c) => ({ ...c, [g]: !c[g] }))}
                  >
                    <span
                      className="w-2.5 h-2.5 rounded-xs shrink-0"
                      style={{ background: meta.color }}
                      aria-hidden="true"
                    />
                    <span id={`tq-group-${g}`} className="text-sm font-semibold">
                      {meta.label}
                    </span>
                    <span className="text-xs text-text-subtle tabular">{rows.length}</span>
                    <span className="ml-auto text-xs text-text-subtle hidden sm:inline">{meta.hint}</span>
                    <Icon name="chevron-down" size="xs" className="car" aria-hidden="true" />
                  </button>
                </h2>
              </div>
            </div>
            {!closed && (
              <div id={`tq-rows-${g}`} className="tq-rows">
                {rows.length === 0 ? (
                  <div role="row">
                    <div role="gridcell" className="px-3.5 py-3.5 text-sm text-text-subtle">
                      {g === "triage" ? "Nothing waiting for a decision." : "Drop here."}
                    </div>
                  </div>
                ) : (
                  rows.map((r) => (
                    <TaskRowView
                      key={r.task_id}
                      r={r}
                      ctx={ctx}
                      drag={drag.source(r)}
                      tabStop={tabStop === `task:${r.task_id}`}
                    />
                  ))
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
// biome-ignore-end lint/a11y/useFocusableInteractive: the grid ends
// biome-ignore-end lint/a11y/useSemanticElements: the grid ends
