import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import type { TaskListRow, TaskStatus } from "../../api/tasks";
import {
  PRIORITY_LABEL,
  STATUS_META,
  initials,
  leaseMinutesLeft,
  posterKind,
  posterName,
  type PosterKind,
} from "../../lib/tasks";

/**
 * The Tasks page's small shapes: the poster avatar, the priority bar, the lease bar (the
 * `tq-*` classes in styles.css), and the kind, status and priority badges. The list, the
 * board and the panel draw them the same way.
 */

/** The inbox's own agent is round on the accent, another agent square on
 *  info, a person by initials on the neutral surface. */
export function Avatar({ name, kind, title }: { name: string; kind: PosterKind; title?: string }) {
  return (
    <span className="tq-av" data-kind={kind} title={title ?? name} aria-hidden="true">
      {initials(name)}
    </span>
  );
}

export function PosterAvatar({
  r,
  ownerAgent,
}: {
  r: Pick<TaskListRow, "from_agent" | "from_subject">;
  ownerAgent: string | undefined;
}) {
  const name = posterName(r);
  return <Avatar name={name} kind={posterKind(r, ownerAgent)} title={name} />;
}

export function PriorityBar({ priority }: { priority: number }) {
  const label = PRIORITY_LABEL[priority] ?? `P${priority}`;
  return <span className="tq-pri" data-p={priority} title={label} role="img" aria-label={label} />;
}

export function KindTag({ kind }: { kind: string }) {
  return <Badge text={<span className="font-mono">{kind}</span>} size="sm" />;
}

/** How much of the lease is left, amber under ten minutes. */
export function LeaseBar({
  leaseUntil,
  leaseSeconds,
  now,
}: {
  leaseUntil: string | null;
  leaseSeconds: number;
  now: number;
}) {
  const left = leaseMinutesLeft(leaseUntil, now);
  if (left === null) return <span className="text-xs text-text-subtle">no lease</span>;
  const total = Math.max(1, leaseSeconds / 60);
  const pct = Math.max(4, Math.min(100, (left / total) * 100));
  return (
    <>
      <span
        className="tq-lease"
        data-low={left <= 10}
        title={`lease ends in ${left} min`}
        role="img"
        aria-label={`lease: ${left} min left`}
      >
        <i style={{ width: `${pct}%` }} />
      </span>
      <span className="tabular" aria-hidden="true">
        {left} min
      </span>
    </>
  );
}

const STATUS_STATE: Record<TaskStatus, BadgeState> = {
  triage: "default",
  todo: "neutral",
  in_progress: "info",
  blocked: "warning",
  deferred: "neutral",
  done: "success",
  wont_do: "disabled",
  failed: "danger",
  cancelled: "disabled",
};

export function StatusPill({ status }: { status: TaskStatus }) {
  const meta = STATUS_META[status] ?? STATUS_META.todo;
  return <Badge text={meta.label} state={STATUS_STATE[status] ?? "neutral"} size="sm" />;
}

export function PriorityBadge({ priority }: { priority: number }) {
  return <Badge text={PRIORITY_LABEL[priority] ?? `P${priority}`} state="neutral" size="sm" />;
}
