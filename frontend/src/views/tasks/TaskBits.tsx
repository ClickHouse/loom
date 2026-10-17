import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import type { TaskListRow, TaskStatus } from "../../api/tasks";
import {
  GROUP_META,
  PRIORITY_LABEL,
  STATUS_META,
  initials,
  leaseMinutesLeft,
  posterKind,
  posterName,
  type Group,
  type PosterKind,
} from "../../lib/tasks";

/**
 * The Tasks page's small shapes: the poster avatar, the priority bar, the lease bar (the
 * `tq-*` classes in styles.css), and the kind, status and priority badges. The list, the
 * board and the panel draw them the same way.
 */

/** The inbox's own agent is round with a stroke, another agent square on
 *  info, a person by initials on the neutral surface. Decorative: the name it stands for
 *  is written beside it. */
export function Avatar({ name, kind }: { name: string; kind: PosterKind }) {
  return (
    <span className="tq-av" data-kind={kind} aria-hidden="true">
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
  return <Avatar name={name} kind={posterKind(r, ownerAgent)} />;
}

export function PriorityBar({ priority }: { priority: number }) {
  const label = PRIORITY_LABEL[priority] ?? `P${priority}`;
  return <span className="tq-pri" data-p={priority} role="img" aria-label={label} />;
}

export function KindTag({ kind }: { kind: string }) {
  return <Badge text={<span className="font-mono">{kind}</span>} size="md" />;
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
  if (left === null) return <span className="text-xs text-text-muted">no lease</span>;
  const total = Math.max(1, leaseSeconds / 60);
  const pct = Math.max(4, Math.min(100, (left / total) * 100));
  return (
    <>
      <span className="tq-lease" data-low={left <= 10} role="img" aria-label={`lease: ${left} min left`}>
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
  return <Badge text={meta.label} state={STATUS_STATE[status] ?? "neutral"} size="md" />;
}

export function PriorityBadge({ priority }: { priority: number }) {
  return <Badge text={PRIORITY_LABEL[priority] ?? `P${priority}`} state="neutral" size="md" />;
}

// A lane's state is a Click UI badge, the one status carrier: Triage asks for a decision
// (info), Waiting is held up (warning); the rest are neutral.
const LANE_STATE: Record<Group, BadgeState> = {
  triage: "info",
  todo: "neutral",
  in_progress: "default",
  waiting: "warning",
  closed: "default",
};

/** A list group's or board column's name, as its state badge. */
export function LaneBadge({ group, id }: { group: Group; id?: string }) {
  return (
    <span id={id} className="inline-flex">
      <Badge text={GROUP_META[group].label} state={LANE_STATE[group]} size="md" />
    </span>
  );
}
