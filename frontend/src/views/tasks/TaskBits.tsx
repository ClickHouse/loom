import { Avatar } from "@clickhouse/click-ui/Avatar";
import { Badge, type BadgeState } from "@clickhouse/click-ui/Badge";
import type { TaskStatus } from "../../api/tasks";
import { GROUP_META, PRIORITY_LABEL, STATUS_META, leaseMinutesLeft, type Group } from "../../lib/tasks";
import { Tip } from "../../components/ui";
import { toast } from "../../components/Toast";

/** Copies a task's id, else shows it in a toast to copy by hand. */
export function copyTaskId(id: string): void {
  navigator.clipboard
    ?.writeText(id)
    .then(() => toast("Task id copied", "ok"))
    .catch(() => toast(id, "ok"));
}

/**
 * The Tasks page's small shapes: the assignee avatar, the lease bar (the `tq-*` classes in
 * styles.css), and the kind, status and priority badges. The list, the board and the
 * panel draw them the same way.
 */

/** Who has the task in hand, as Click UI's neutral Avatar: the name is its tooltip and
 *  what a screen reader reads (the initials are decoration). */
export function Assignee({ name, label }: { name: string; label: string }) {
  return (
    <Tip content={label}>
      <span className="tq-assignee relative z-[1]">
        <Avatar text={name.replace(/^(sa|human|process):/, "").replace(/[-_.@]+/g, " ")} aria-hidden="true" />
        <span className="sr-only">{label}</span>
      </span>
    </Tip>
  );
}

/** A list row's priority: "P1" in a neutral badge, read in full ("P1 urgent"). */
export function PriorityBar({ priority }: { priority: number }) {
  return (
    <>
      <span aria-hidden="true">
        <Badge text={`P${priority}`} state="neutral" size="sm" />
      </span>
      <span className="sr-only">{PRIORITY_LABEL[priority] ?? `P${priority}`}</span>
    </>
  );
}

/** A task's kind: a neutral badge in plain text (a label, not code). */
export function KindTag({ kind }: { kind: string }) {
  return <Badge text={kind} state="neutral" size="sm" />;
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
  // The bar is the lease's share left, against the inbox's lease length.
  const total = Math.max(1, leaseSeconds / 60);
  const pct = Math.max(4, Math.min(100, (left / total) * 100));
  return (
    <>
      <span className="tq-lease" data-low={left <= 10} role="img" aria-label={`lease: ${left} min left`}>
        <i style={{ width: `${pct}%` }} />
      </span>
      <span className="tabular" aria-hidden="true">
        {left}m left
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
  const { label } = STATUS_META[status] ?? STATUS_META.todo;
  // Sentence case, as every label: "In progress".
  return <Badge text={label[0].toUpperCase() + label.slice(1)} state={STATUS_STATE[status] ?? "neutral"} size="md" />;
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
