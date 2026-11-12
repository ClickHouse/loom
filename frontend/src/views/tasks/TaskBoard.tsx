import { memo, useRef, useState, type HTMLAttributes, type KeyboardEvent } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Title } from "@clickhouse/click-ui/Title";
import type { TaskListRow } from "../../api/tasks";
import { GROUPS, assigneeOf, type Group } from "../../lib/tasks";
import { Assignee, LaneBadge, PriorityBar } from "./TaskBits";
import { LANE_ROWS, NO_MATCH, SublineView, type RowContext } from "./TaskList";
import { useTaskDrag } from "./drag";
import { ListTime } from "../../components/ui/ListTime";

/**
 * The board: the list's five groups as columns that scroll sideways, compact cards — the
 * priority and the title, the kind and the state's one line, the assignee and the age. A
 * column lists 20 cards, then "Show all N". Board and list share the rows, the filters and
 * the selection; switching keeps the open panel.
 *
 * A writer drags a card onto another column to move it: the move is the one `dropAction`
 * names (a reason still opens its dialog). While a card is held, the columns it may land
 * in are marked and the others dimmed. The panel's buttons are the keyboard path.
 */

// What an empty column says: what would put a task there.
const EMPTY_COLUMN: Record<Group, string> = {
  triage: "Nothing waiting for a decision.",
  todo: "Accepted tasks queue here for an agent.",
  in_progress: "Nothing is running.",
  waiting: "Blocked and deferred tasks wait here.",
  closed: "Nothing closed yet.",
};

function Card({ r, ctx, drag }: { r: TaskListRow; ctx: RowContext; drag: HTMLAttributes<HTMLElement> }) {
  const who = assigneeOf(r, ctx.ownerAgentOf(r.inbox));
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
      <span className="flex items-start gap-2">
        <PriorityBar priority={r.priority} />
        <span className="font-medium text-sm min-w-0 [overflow-wrap:anywhere]">{r.title}</span>
      </span>
      <SublineView r={r} ctx={ctx} plain />
      <span className="flex justify-between items-center gap-2 text-xs text-text-muted">
        {who ? <Assignee name={who.name} label={who.label} /> : <span />}
        <ListTime time={r.created_at} quiet />
      </span>
    </div>
  );
}

// Memoised: a keystroke in the toolbar's search renders the page, not every card.
export const TaskBoard = memo(function TaskBoard({
  groups,
  ctx,
  filtering,
}: {
  groups: Record<Group, TaskListRow[]>;
  ctx: RowContext;
  /** A filter is on: an empty column may hold tasks it hides. */
  filtering: boolean;
}) {
  const drag = useTaskDrag(ctx.writeReason === null, ctx.onDrop);
  const [all, setAll] = useState<ReadonlySet<Group>>(new Set());
  const board = useRef<HTMLElement>(null);
  return (
    <section ref={board} className="tq-board" aria-label="Tasks board">
      {GROUPS.map((g) => {
        const rows = groups[g];
        const listed = all.has(g) ? rows : rows.slice(0, LANE_ROWS);
        return (
          <section key={g} className="tq-col" aria-labelledby={`tq-col-${g}`} {...drag.target(g)}>
            <div className="px-3 py-2 border-b border-border">
              <Title type="h2" size="sm" id={`tq-col-${g}`} className="flex items-center gap-2">
                <LaneBadge group={g} />
                <span className="ml-auto text-xs font-medium text-text-muted tabular">{rows.length}</span>
              </Title>
            </div>
            <div className="p-2 flex flex-col gap-2">
              {rows.length === 0 ? (
                <div className="p-4 text-xs text-text-muted">{filtering ? NO_MATCH : EMPTY_COLUMN[g]}</div>
              ) : (
                listed.map((r) => <Card key={r.task_id} r={r} ctx={ctx} drag={drag.source(r)} />)
              )}
              {listed.length < rows.length && (
                <Button
                  type="empty"
                  htmlType="button"
                  onClick={() => {
                    const first = rows[LANE_ROWS];
                    setAll((s) => new Set(s).add(g));
                    // Focus only: selecting the card would open it in place of the open task.
                    if (first)
                      window.setTimeout(
                        () =>
                          board.current
                            ?.querySelector<HTMLElement>(`[data-task-id="${CSS.escape(first.task_id)}"]`)
                            ?.focus(),
                        0,
                      );
                  }}
                >
                  {`Show all ${rows.length}`}
                </Button>
              )}
            </div>
          </section>
        );
      })}
    </section>
  );
});
