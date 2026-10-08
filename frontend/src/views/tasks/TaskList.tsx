import { useState, type HTMLAttributes, type KeyboardEvent, type MouseEvent } from "react";
import { ChevronDown } from "lucide-react";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, GROUP_META, rel, sublineOf, type Group, type Subline } from "../../lib/tasks";
import { Avatar, KindTag, LeaseBar, PosterAvatar, PriorityBar } from "./TaskBits";
import { useTaskDrag } from "./drag";

/**
 * The list: five collapsible groups by what happens next — Triage, To do, In progress,
 * Waiting, Closed — each with a swatch, a count and a hint. A row carries the decision:
 * the priority bar, the kind, the title, the one line that matters for its state, and a
 * meta cell — inline Accept / Won't do in Triage, the owner and lease bar in progress,
 * the poster and age otherwise. Rows are `role=row` with a tab stop (they hold buttons,
 * so they are not buttons themselves). A writer drags a row onto another group to move
 * it (`useTaskDrag`); while a row is held, empty and hidden groups show as drop targets.
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
            <button
              type="button"
              className="tq-mini"
              data-ok="true"
              onClick={(e) => {
                stop(e);
                ctx.onAccept(r);
              }}
              aria-label={`Accept ${r.title}`}
            >
              Accept
            </button>
            <button
              type="button"
              className="tq-mini"
              onClick={(e) => {
                stop(e);
                ctx.onWontDo(r);
              }}
              aria-label={`Won't do ${r.title}`}
            >
              Won't do…
            </button>
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
      <PriorityBar priority={r.priority} />
      <span className="min-w-0" role="cell">
        <span className="t">
          <KindTag kind={r.kind} />
          <span className="title text-base" title={r.title}>
            {r.title}
          </span>
        </span>
        <SublineView s={sublineOf(r, ctx.now, ctx.progressNotes.get(r.task_id))} />
      </span>
      <span role="cell">
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
          <section
            key={g}
            className="tq-group"
            data-collapsed={isCollapsed}
            aria-labelledby={`tq-group-${g}`}
            {...drag.target(g)}
          >
            <button
              type="button"
              className="tq-ghead"
              aria-expanded={!isCollapsed}
              aria-controls={`tq-rows-${g}`}
              onClick={() => setCollapsed((c) => ({ ...c, [g]: !c[g] }))}
            >
              <span
                className="w-2.5 h-2.5 rounded-[3px] shrink-0"
                style={{ background: meta.color }}
                aria-hidden="true"
              />
              <h3 id={`tq-group-${g}`} className="text-sm font-semibold m-0">
                {meta.label}
              </h3>
              <span
                className="text-xs text-[var(--color-text-subtle)] tabular"
                aria-label={`${rows.length} in ${meta.label}`}
              >
                {rows.length}
              </span>
              <span className="ml-auto text-xs text-[var(--color-text-subtle)] hidden sm:inline">{meta.hint}</span>
              <ChevronDown size={14} className="car" aria-hidden="true" />
            </button>
            {!isCollapsed && (
              <div id={`tq-rows-${g}`} className="tq-rows" role="rowgroup">
                {rows.length === 0 ? (
                  <div className="px-3.5 py-3.5 text-sm text-[var(--color-text-subtle)]">
                    {g === "triage" ? "Nothing waiting for a decision." : "Drop here."}
                  </div>
                ) : (
                  rows.map((r) => <TaskRowView key={r.task_id} r={r} ctx={ctx} drag={drag.source(r)} />)
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
