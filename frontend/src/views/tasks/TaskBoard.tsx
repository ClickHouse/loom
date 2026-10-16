import type { HTMLAttributes, KeyboardEvent } from "react";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, GROUP_META, leaseMinutesLeft, posterName, rel, sublineOf, type Group } from "../../lib/tasks";
import { Avatar, KindTag, PosterAvatar } from "./TaskBits";
import type { RowContext } from "./TaskList";
import { useTaskDrag } from "./drag";

/**
 * The board: the list's five groups as columns that scroll sideways, compact cards —
 * priority dot, kind, title, the state's one line, poster or owner, age. Board and list
 * share the rows, the filters and the selection; switching keeps the open panel.
 *
 * A writer drags a card onto another column to move it: the move is the one `dropAction`
 * names (a reason still opens its dialog). While a card is held, the columns it may land
 * in are marked and the others dimmed. The panel's buttons are the keyboard path.
 */
function Card({ r, ctx, drag }: { r: TaskListRow; ctx: RowContext; drag: HTMLAttributes<HTMLElement> }) {
  const s = sublineOf(r, ctx.now, ctx.progressNotes.get(r.task_id));
  const running = r.status === "in_progress";
  const left = running ? leaseMinutesLeft(r.lease_until, ctx.now) : null;
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) {
      e.preventDefault();
      ctx.onOpen(r);
    }
  };
  // A div, not a button: Firefox never starts a drag on a <button>.
  return (
    // biome-ignore lint/a11y/useSemanticElements: a draggable card; Firefox never starts a drag on a <button>
    <div
      role="button"
      tabIndex={0}
      className="tq-card"
      aria-current={ctx.selectedId === r.task_id ? "true" : undefined}
      data-task-id={r.task_id}
      onClick={() => ctx.onOpen(r)}
      onKeyDown={onKey}
      {...drag}
    >
      <span className="flex items-center gap-2">
        <span className="tq-pdot" data-p={r.priority} aria-label={`P${r.priority}`} role="img" />
        <KindTag kind={r.kind} />
      </span>
      <span className="font-medium text-sm">{r.title}</span>
      {s && (
        <span className="text-xs text-text-muted italic">
          {s.text}
          {s.href ? ` ${s.href.replace(/^https?:\/\/(www\.)?(github\.com\/)?/, "")}` : ""}
          {s.suffix ? ` ${s.suffix}` : ""}
        </span>
      )}
      <span className="flex justify-between items-center gap-2 text-xs text-text-subtle">
        <span className="tq-who min-w-0">
          {running ? (
            <>
              <Avatar name={r.owner || "?"} kind="me" />
              <span className="truncate">
                {r.owner}
                {left !== null ? ` · ${left} min left` : ""}
              </span>
            </>
          ) : (
            <>
              <PosterAvatar r={r} ownerAgent={ctx.ownerAgentOf(r.inbox)} />
              <span className="truncate">{posterName(r)}</span>
            </>
          )}
        </span>
        <time className="tabular shrink-0" dateTime={r.created_at}>
          {rel(r.created_at, ctx.now)}
        </time>
      </span>
    </div>
  );
}

export function TaskBoard({
  groups,
  ctx,
  showClosed,
}: {
  groups: Record<Group, TaskListRow[]>;
  ctx: RowContext;
  /** Closed rows are filtered out unless shown; the column stays as a drop target. */
  showClosed: boolean;
}) {
  const drag = useTaskDrag(ctx.canWrite, ctx.onDrop);
  return (
    <section className="tq-board" aria-label="Tasks board">
      {GROUPS.map((g) => {
        const rows = groups[g];
        const meta = GROUP_META[g];
        return (
          <section key={g} className="tq-col" aria-labelledby={`tq-col-${g}`} {...drag.target(g)}>
            <h2
              id={`tq-col-${g}`}
              className="text-sm font-semibold m-0 px-3 py-2.5 border-b border-border flex items-center gap-2"
            >
              <span className="inline-block w-2 h-2 rounded-xs" style={{ background: meta.color }} aria-hidden="true" />
              {meta.label}
              <span className="ml-auto text-xs font-medium text-text-subtle tabular">{rows.length}</span>
            </h2>
            <div className="p-2 flex flex-col gap-2">
              {rows.length === 0 ? (
                <div className="p-4 text-xs text-text-subtle">
                  {g === "closed" && !showClosed ? "closed tasks are hidden — show them in the rail" : "nothing here"}
                </div>
              ) : (
                rows.map((r) => <Card key={r.task_id} r={r} ctx={ctx} drag={drag.source(r)} />)
              )}
            </div>
          </section>
        );
      })}
    </section>
  );
}
