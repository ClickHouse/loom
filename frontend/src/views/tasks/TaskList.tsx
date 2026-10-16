import { useState, type HTMLAttributes, type KeyboardEvent, type MouseEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon } from "@clickhouse/click-ui/Icon";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, GROUP_META, rel, sublineOf, type Group, type Subline } from "../../lib/tasks";
import { Avatar, KindTag, LeaseBar, PosterAvatar, PriorityBar } from "./TaskBits";
import { useTaskDrag } from "./drag";

/**
 * The list: five collapsible groups by what happens next — Triage, To do, In progress,
 * Waiting, Closed — each with a swatch, a count and a hint. A row carries the decision:
 * the priority bar, the kind, the title, the one line that matters for its state, and a
 * meta cell — inline Accept / Won't do in Triage, the owner and lease bar in progress,
 * the poster and age otherwise. The DOM is a grid: each group is a rowgroup whose first
 * row holds the collapse button, then its task rows. Rows have a tab stop (they hold
 * buttons, so they are not buttons themselves). A writer drags a row onto another group to
 * move it (`useTaskDrag`); while a row is held, empty and hidden groups show as drop targets.
 */
export interface RowContext {
  now: number;
  ownerAgentOf: (inbox: string) => string | undefined;
  leaseSecondsOf: (inbox: string) => number;
  /** The latest progress note per task, from the histories the page has read. */
  progressNotes: ReadonlyMap<string, string>;
  selectedId: string | null;
  onOpen: (r: TaskListRow) => void;
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
        <a href={s.href} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
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
              type="primary"
              htmlType="button"
              onClick={(e) => {
                stop(e);
                ctx.onAccept(r);
              }}
              aria-label={`Accept ${r.title}`}
            >
              Accept
            </Button>
            <Button
              type="secondary"
              htmlType="button"
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
        <span className="tabular" title={r.created_at}>
          {rel(r.created_at, ctx.now)}
        </span>
      </span>
    );
  }
  return (
    <span className="tq-meta">
      <span className="tq-who">
        <PosterAvatar r={r} ownerAgent={ctx.ownerAgentOf(r.inbox)} />
      </span>
      <span className="tabular" title={r.created_at}>
        {rel(r.created_at, ctx.now)}
      </span>
    </span>
  );
}

// biome-ignore-start lint/a11y/useSemanticElements: a div grid — rows are draggable and grouped into collapsible groups, which a <table> cannot hold
// biome-ignore-start lint/a11y/useFocusableInteractive: each task row is a tab stop; cells, header and placeholder rows are read, not visited
function TaskRowView({ r, ctx, drag }: { r: TaskListRow; ctx: RowContext; drag: HTMLAttributes<HTMLElement> }) {
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
      tabIndex={0}
      aria-selected={selected}
      aria-label={r.title}
      data-task-id={r.task_id}
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
          <span className="title text-sm" title={r.title}>
            {r.title}
          </span>
        </span>
        <SublineView s={sublineOf(r, ctx.now, ctx.progressNotes.get(r.task_id))} />
      </span>
      <span role="gridcell">
        <Meta r={r} ctx={ctx} />
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
  const drag = useTaskDrag(ctx.canWrite, ctx.onDrop);
  return (
    <div role="grid" aria-label="Tasks by what happens next" className="min-w-0">
      {GROUPS.map((g) => {
        const rows = groups[g];
        // A held row needs every group it could land in, even an empty or hidden one.
        if (!drag.holding && rows.length === 0 && g !== "triage") return null;
        if (!drag.holding && g === "closed" && !showClosed) return null;
        const meta = GROUP_META[g];
        const isCollapsed = collapsed[g] && !forceOpen;
        return (
          <div
            key={g}
            role="rowgroup"
            className="tq-group"
            data-collapsed={isCollapsed}
            aria-labelledby={`tq-group-${g}`}
            {...drag.target(g)}
          >
            <div role="row">
              <div role="gridcell">
                <button
                  type="button"
                  className="tq-ghead"
                  aria-expanded={!isCollapsed}
                  aria-controls={isCollapsed ? undefined : `tq-rows-${g}`}
                  onClick={() => setCollapsed((c) => ({ ...c, [g]: !c[g] }))}
                >
                  <span
                    className="w-2.5 h-2.5 rounded-xs shrink-0"
                    style={{ background: meta.color }}
                    aria-hidden="true"
                  />
                  <h3 id={`tq-group-${g}`} className="text-sm font-semibold m-0">
                    {meta.label}
                  </h3>
                  <span className="text-xs text-text-subtle tabular">{rows.length}</span>
                  <span className="ml-auto text-xs text-text-subtle hidden sm:inline">{meta.hint}</span>
                  <Icon name="chevron-down" size="xs" className="car" aria-hidden="true" />
                </button>
              </div>
            </div>
            {!isCollapsed && (
              <div id={`tq-rows-${g}`} className="tq-rows">
                {rows.length === 0 ? (
                  <div role="row">
                    <div role="gridcell" className="px-3.5 py-3.5 text-sm text-text-subtle">
                      {g === "triage" ? "Nothing waiting for a decision." : "Drop here."}
                    </div>
                  </div>
                ) : (
                  rows.map((r) => <TaskRowView key={r.task_id} r={r} ctx={ctx} drag={drag.source(r)} />)
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
