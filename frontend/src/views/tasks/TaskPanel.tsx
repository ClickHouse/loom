import { forwardRef, useEffect, useRef, useState, type ReactNode, type Ref } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Button } from "@clickhouse/click-ui/Button";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { Title } from "@clickhouse/click-ui/Title";
import {
  getTask,
  listPolicies,
  listTasks,
  taskHistory,
  type Caller,
  type Inbox,
  type PosterPolicy,
  type TaskEvent,
  type TaskListRow,
  type TaskRow,
} from "../../api/tasks";
import {
  EVENT_LABEL,
  actionsFor,
  actorKind,
  groupOf as groupOfStatus,
  actorName,
  briefSections,
  collapseHeartbeats,
  evidenceStatus,
  inputKind,
  issueHref,
  leaseMinutesLeft,
  menuLabel,
  posterKind,
  posterName,
  rel,
  resolvePolicy,
  shortId,
  tallyRecord,
  type Group,
  type PosterRecord,
  type TaskAction,
} from "../../lib/tasks";
import { humanizeError } from "../../lib/errorWords";
import { IconButton } from "../../components/IconButton";
import { Alert, ExternalLink, Loading, Stat, Tip } from "../../components/ui";
import { ListTime } from "../../components/ui/ListTime";
import { KindTag, PriorityBadge, StatusPill, copyTaskId } from "./TaskBits";
import { MenuTrigger } from "../../components/ui/Menu";
import { keepOpenOnToasts } from "../../components/Toast";

/**
 * The detail panel: what it is, what to do, then the document. Header — status, priority,
 * kind, the title, who handed it to which inbox and when (with the owner and lease of a
 * running task, the resume time of a deferred one). Under it the action bar, in view at
 * any height: only the moves the status allows, at most one primary and one secondary,
 * the rest under More. Body — why a task waits or ended, first; for a triage task the
 * poster's record and the trust rule; then the brief as sections (Objective, Acceptance
 * criteria, Inputs as key/value with links, Constraints in a warning tone), the outcome,
 * and the timeline from `task.history` with people, agents and the sweeper told apart.
 * The side panel and the sheet share this layout.
 *
 * Reads `task.get` + `task.history` once per opened task (and again after
 * an action, via `reloadKey`); a triage task adds one `task.list posted_by`
 * and one `inbox.policy_list`. Actions are the page's: the panel says which.
 */
// A status whose reason is the first thing to read: it waits on something, or it ended.
const WAITS_OR_ENDED: ReadonlySet<Group> = new Set(["waiting", "closed"]);

interface Props {
  identity: Caller;
  row: TaskListRow;
  inboxes: Inbox[];
  canWrite: boolean;
  now: number;
  /** Bumped by the page after an action; the panel re-reads. */
  reloadKey: number;
  onClose: () => void;
  onAction: (action: TaskAction, task: TaskListRow | TaskRow) => void;
  onOpenTask: (taskId: string) => void;
  /** The history landed: the page keeps the latest progress note per row. */
  onHistory?: (taskId: string, events: TaskEvent[]) => void;
  /** Under 1280 px the panel is a modal sheet: the page renders it inside a Radix
   *  `Dialog.Root` + `Portal`, and the panel is the dialog's content, titled by its
   *  heading. Opening focuses the sheet; closing returns the focus to the task's row. */
  sheet?: boolean;
}

const ACTOR_KIND_LABEL: Record<ReturnType<typeof actorKind>, string> = {
  human: "a person",
  sweeper: "the lease sweeper",
  bot: "an agent",
};
const ACTOR_ICON: Record<ReturnType<typeof actorKind>, IconName> = { human: "user", sweeper: "clock", bot: "cpu" };

const URL_RE = /^https?:\/\/\S+$/i;

function InputValue({ v }: { v: unknown }) {
  if (Array.isArray(v)) {
    return (
      <ul className="m-0 pl-4 list-disc">
        {v.map((x, i) => (
          <li key={i}>
            <InputValue v={x} />
          </li>
        ))}
      </ul>
    );
  }
  if (v && typeof v === "object")
    return <pre className="text-xs font-mono whitespace-pre-wrap break-all m-0">{JSON.stringify(v, null, 1)}</pre>;
  const s = String(v);
  switch (inputKind(s)) {
    case "url":
      return (
        <ExternalLink href={s} className="break-all">
          {s.replace(/^https?:\/\//, "")}
        </ExternalLink>
      );
    case "issue": {
      const href = issueHref(s);
      return href ? (
        <ExternalLink href={href} size="sm">
          {s}
        </ExternalLink>
      ) : (
        <span className="font-mono text-xs">{s}</span>
      );
    }
    case "span":
    case "file":
    case "hex":
      return <span className="font-mono text-xs">{s}</span>;
    default:
      return <span className="whitespace-pre-wrap break-words">{s}</span>;
  }
}

// Each status allows at most one of these: the bar's primary, last on the right.
// A button with a key names it in its tooltip ("Accept · A", after a `tip` that explains
// more than the label); the help dialog lists them all.
const PRIMARY_ACTIONS: ActionButton[] = [
  { action: "accept", label: "Accept", key: "a" },
  { action: "start", label: "Start", tip: "Claim it: to do → in progress", key: "s" },
  { action: "complete", label: "Mark done…", key: "c" },
  { action: "unblock", label: "Unblock" },
  { action: "resume", label: "Resume now" },
];
// The one secondary beside it, the first of these the status allows: the other way the
// task usually goes (fail a run, park a queued task, decline the rest, reopen a closed one).
const SECONDARY_ACTIONS: ActionButton[] = [
  { action: "fail", label: "Fail…" },
  { action: "defer", label: "Defer…", key: "d" },
  { action: "wont_do", label: "Won't do…", key: "x" },
  { action: "reopen", label: "Reopen…" },
];
// The page's keys do nothing while the panel is a sheet (a dialog holds them off), so a
// sheet's tooltip names no key.
export function actionTip(b: ActionButton, sheet: boolean): string {
  const key = b.key && !sheet ? b.key.toUpperCase() : "";
  return [b.tip ?? (key ? b.label.replace(/…$/, "") : ""), key].filter(Boolean).join(" · ");
}
// Everything else the status allows, under More, in this order.
const MENU_ACTIONS: TaskAction[] = ["block", "defer", "wont_do", "edit", "reassign", "cancel"];

interface ActionButton {
  action: TaskAction;
  label: string;
  tip?: string;
  key?: string;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="tq-sec">
      <Title type="h3" size="sm" className="mb-1.5">
        {title}
      </Title>
      {children}
    </div>
  );
}

function Track({
  record,
  policy,
  task,
}: {
  record: PosterRecord | null | undefined;
  policy: PosterPolicy | null | undefined;
  task: TaskRow;
}) {
  const ev = policy ? evidenceStatus(policy.require_evidence, task.brief) : null;
  return (
    <Section title="Poster's record in this inbox">
      {record === undefined && <Loading layout="inline" label="Loading the poster's record" />}
      {record === null && <div className="text-xs text-text-muted">Not readable</div>}
      {record && (
        // biome-ignore lint/a11y/useSemanticElements: a labelled cluster of counts, not form fields
        <div
          className="grid grid-cols-2 gap-2 min-[420px]:grid-cols-4"
          role="group"
          aria-label={`${record.total}${record.truncated ? "+" : ""} tasks from this poster: ${record.done} done, ${record.wont_do} won't do, ${record.failed} failed, ${record.open} open`}
        >
          {(
            [
              ["Done", record.done],
              ["Won't do", record.wont_do],
              ["Failed", record.failed],
              ["Open", record.open],
            ] as Array<[string, number]>
          ).map(([l, n]) => (
            <Stat key={l} label={l} value={n} />
          ))}
        </div>
      )}
      <p className="text-xs text-text-muted mt-1.5 m-0">
        {policy === undefined && "Rule: …"}
        {policy === null && (
          <>
            Rule: none — in review because <span className="font-mono">{posterName(task)}</span> is not a writer here
          </>
        )}
        {policy && (
          <>
            Rule: <span className="font-mono">{policy.mode}</span> for{" "}
            <span className="font-mono">{policy.poster}</span>
            {policy.kind !== "*" && (
              <>
                {" "}
                × <span className="font-mono">{policy.kind}</span>
              </>
            )}
            {ev && ev.present.length > 0 && (
              <>
                {" "}
                · evidence <span className="font-mono">{ev.present.join(", ")}</span> present
              </>
            )}
            {ev && ev.missing.length > 0 && (
              <>
                {" "}
                ·{" "}
                <span className="text-warn">
                  missing <span className="font-mono">{ev.missing.join(", ")}</span>
                </span>
              </>
            )}
          </>
        )}
      </p>
    </Section>
  );
}

function Timeline({ events }: { events: TaskEvent[] | null }) {
  if (events === null) return <Loading layout="inline" label="Loading the timeline" />;
  if (events.length === 0) return <div className="text-xs text-text-muted">No events recorded.</div>;
  const asc = [...events].sort((a, b) => a.seq - b.seq);
  const rows = collapseHeartbeats(asc).reverse();
  return (
    <ul className="tq-tl">
      {rows.map((e) => {
        const kind = actorKind(e.actor, e.agent);
        const text = e.reason || e.note;
        return (
          <li key={e.seq}>
            <span className="ic" data-kind={kind} role="img" aria-label={ACTOR_KIND_LABEL[kind]}>
              <Icon name={ACTOR_ICON[kind]} size="xs" aria-hidden="true" />
            </span>
            <span className="text-text-muted min-w-0">
              <b className="font-medium text-text">
                {e.collapsed && e.collapsed > 1
                  ? `${e.collapsed} heartbeats`
                  : (EVENT_LABEL[e.event] ?? e.event.replace(/_/g, " "))}
              </b>
              {" · "}
              {actorName(e.actor, e.agent)}
              {e.from_status && e.to_status && e.from_status !== e.to_status && (
                <span className="text-text-muted">
                  {" "}
                  · {e.from_status} → {e.to_status}
                </span>
              )}
              {text && (
                <>
                  <br />
                  <span className="italic break-words">{text}</span>
                </>
              )}
            </span>
            <span className="text-xs text-text-muted">
              <ListTime time={e.ts} quiet />
            </span>
          </li>
        );
      })}
    </ul>
  );
}

interface MenuItem {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}

function Menu({ items }: { items: MenuItem[] }) {
  return (
    <Dropdown modal={false}>
      <MenuTrigger>
        <IconButton icon="dots-horizontal" label="More actions" />
      </MenuTrigger>
      <Dropdown.Content align="end">
        {items.map((it) => (
          <Dropdown.Item
            key={it.label}
            type={it.danger ? "danger" : "default"}
            disabled={it.disabled}
            onSelect={it.onClick}
          >
            {it.label}
          </Dropdown.Item>
        ))}
      </Dropdown.Content>
    </Dropdown>
  );
}

// A ref: under a Radix Dialog.Portal (the sheet), Radix tracks its child by one.
export const TaskPanel = forwardRef<HTMLElement, Props>(function TaskPanel(
  { identity, row, inboxes, canWrite, now, reloadKey, onClose, onAction, onOpenTask, onHistory, sheet = false },
  ref,
) {
  const [task, setTask] = useState<TaskRow | null>(null);
  const [events, setEvents] = useState<TaskEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<PosterPolicy | null | undefined>(undefined);
  const [record, setRecord] = useState<PosterRecord | null | undefined>(undefined);
  const onHistoryRef = useRef(onHistory);
  onHistoryRef.current = onHistory;

  useEffect(() => {
    let live = true;
    setError(null);
    if (reloadKey === 0) {
      setTask(null);
      setEvents(null);
      setPolicy(undefined);
      setRecord(undefined);
    }
    Promise.all([getTask(identity, row.task_id), taskHistory(identity, row.task_id)])
      .then(([t, ev]) => {
        if (!live) return;
        setTask(t);
        setEvents(ev);
        onHistoryRef.current?.(row.task_id, ev);
        if (!t) setError("This task is not visible to you, or no longer exists.");
      })
      .catch((e) => {
        if (live) {
          setError(humanizeError(e, { read: true }));
          setEvents([]);
        }
      });
    return () => {
      live = false;
    };
  }, [row.task_id, identity, reloadKey]);

  // The poster's record and the trust rule are read only for a task that
  // waits on a decision, once per opened task.
  const triageKey = task?.status === "triage" ? `${task.task_id}::${task.from_subject || task.from_agent}` : "";
  useEffect(() => {
    if (!task || !triageKey) return;
    let live = true;
    listPolicies(identity, task.inbox)
      .then((ps) => {
        if (live) setPolicy(resolvePolicy(ps, task));
      })
      .catch(() => {
        if (live) setPolicy(null);
      });
    listTasks(identity, {
      inbox: task.inbox,
      postedBy: task.from_subject || task.from_agent,
      fields: ["status", "kind", "created_at"],
      limit: 500,
    })
      .then((r) => {
        if (live) setRecord(tallyRecord(r.tasks, r.truncated));
      })
      .catch(() => {
        if (live) setRecord(null);
      });
    return () => {
      live = false;
    };
    // Keyed by task + poster: a reload hands back a new `task` object that must not refetch these.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [triageKey, identity]);

  // The read found no task: the panel says so and offers nothing to wait on or act on.
  const missing = task === null && error !== null;
  const current: TaskRow | TaskListRow = task ?? row;
  const inbox = inboxes.find((i) => i.inbox === current.inbox);
  const handed = posterKind(current, inbox?.owner_agent) !== "me";
  const allowed = canWrite ? new Set(actionsFor(current.status)) : new Set<TaskAction>();
  const brief = briefSections(task?.brief);
  const outcome = task?.outcome && Object.keys(task.outcome).length > 0 ? task.outcome : null;
  const act = (a: TaskAction) => onAction(a, a === "edit" && task ? task : current);
  const primary = PRIMARY_ACTIONS.find((p) => allowed.has(p.action));
  const secondary = SECONDARY_ACTIONS.find((p) => allowed.has(p.action));
  const copyId = () => copyTaskId(current.task_id);

  const menu: MenuItem[] = MENU_ACTIONS.filter((a) => allowed.has(a) && a !== secondary?.action).map((a) => ({
    label: menuLabel(a),
    onClick: () => act(a),
    disabled: a === "edit" && !task,
    danger: a === "cancel",
  }));
  menu.push({ label: "Copy id", onClick: copyId });

  const lease = current.status === "in_progress" ? leaseMinutesLeft(current.lease_until, now) : null;
  const reasonTitle =
    current.status === "blocked"
      ? `Blocked on ${current.blocked_on || "a person"}`
      : current.status === "deferred"
        ? `Deferred${current.resume_at ? ` · resumes ${rel(current.resume_at, now)}` : ""}`
        : "Reason";
  const showReason = !!current.reason && WAITS_OR_ENDED.has(groupOfStatus(current.status));
  const parentTaskId = current.parent_task_id;

  const titleClass = "text-lg font-semibold m-0 mb-1 [text-wrap:balance] break-words";
  const body = (
    <>
      <div className="px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2 flex-wrap mb-2">
          <StatusPill status={current.status} />
          <PriorityBadge priority={current.priority} />
          <KindTag kind={current.kind} />
          <span className="flex-1" />
          <IconButton icon="cross" label="Close" onClick={onClose} />
        </div>
        {/* tabIndex -1: the page puts focus here when the button pressed went away with the move it made. */}
        {sheet ? (
          <RadixDialog.Title className={titleClass} tabIndex={-1} data-panel-title="">
            {current.title}
          </RadixDialog.Title>
        ) : (
          <h2 className={titleClass} tabIndex={-1} data-panel-title="">
            {current.title}
          </h2>
        )}
        <div className="text-xs text-text-muted flex gap-x-1.5 gap-y-0.5 items-center flex-wrap">
          {handed ? (
            <>
              <span className="font-medium">from {posterName(current)}</span>
              <span className="inline-flex items-center gap-1">
                <Icon name="arrow-right" size="xs" aria-hidden="true" />
                <span className="sr-only">to </span>
                {current.inbox}
              </span>
            </>
          ) : (
            <span className="font-medium">this agent's own task</span>
          )}
          <span>
            · posted <ListTime time={current.created_at} />
          </span>
          {current.status === "in_progress" && (
            <span>
              · claimed by {current.owner || "—"}
              {lease !== null && <> · lease ends {rel(current.lease_until, now)}</>}
            </span>
          )}
          {current.status === "deferred" && current.resume_at && <span>· resumes {rel(current.resume_at, now)}</span>}
          {parentTaskId && (
            <Button type="empty" htmlType="button" iconLeft="git-merge" onClick={() => onOpenTask(parentTaskId)}>
              Reassigned from {shortId(parentTaskId)}
            </Button>
          )}
        </div>
        {!missing && (
          // biome-ignore lint/a11y/useSemanticElements: a labelled cluster of action buttons, not form fields
          <div className="mt-3 flex gap-2 items-center justify-end flex-wrap" role="group" aria-label="Actions">
            <Menu items={menu} />
            {secondary && (
              <Tip content={actionTip(secondary, sheet)} disabled={!actionTip(secondary, sheet)}>
                <Button
                  type="secondary"
                  htmlType="button"
                  aria-keyshortcuts={sheet ? undefined : secondary.key}
                  onClick={() => act(secondary.action)}
                >
                  {secondary.label}
                </Button>
              </Tip>
            )}
            {primary && (
              <Tip content={actionTip(primary, sheet)} disabled={!actionTip(primary, sheet)}>
                <Button
                  type="primary"
                  htmlType="button"
                  aria-keyshortcuts={sheet ? undefined : primary.key}
                  onClick={() => act(primary.action)}
                >
                  {primary.label}
                </Button>
              </Tip>
            )}
          </div>
        )}
      </div>

      <div className="overflow-y-auto px-4 pt-1 pb-4 flex-1 min-h-0">
        {error && <Alert role="alert" state="danger" size="small" text={error} className="my-2" />}
        {!missing && (
          <>
            {showReason && (
              <Section title={reasonTitle}>
                <p className="m-0 text-text-muted whitespace-pre-wrap">{current.reason}</p>
              </Section>
            )}
            {task?.status === "triage" && <Track record={record} policy={policy} task={task} />}
            <Section title="Objective">
              {task ? (
                <p className="m-0 text-text-muted whitespace-pre-wrap">
                  {brief.objective || <span className="text-text-muted">The poster left the objective empty.</span>}
                </p>
              ) : (
                <Loading layout="inline" label="Loading the brief" />
              )}
            </Section>
            {brief.criteria.length > 0 && (
              <Section title="Acceptance criteria">
                <ul className="m-0 pl-4.5 text-text-muted list-disc">
                  {brief.criteria.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </Section>
            )}
            {brief.inputs.length > 0 && (
              <Section title="Inputs">
                <dl className="tq-kv">
                  {brief.inputs.map(([k, v]) => (
                    <div key={k} className="contents">
                      <dt>{k.replace(/_/g, " ")}</dt>
                      <dd>
                        <InputValue v={v} />
                      </dd>
                    </div>
                  ))}
                </dl>
              </Section>
            )}
            {brief.constraints && (
              <Section title="Constraints">
                <div className="tq-constraint">{brief.constraints}</div>
              </Section>
            )}
            {outcome && (
              <Section title="Outcome">
                <dl className="tq-kv tq-outcome">
                  {Object.entries(outcome).map(([k, v]) => (
                    <div key={k} className="contents">
                      <dt>{k.replace(/_/g, " ")}</dt>
                      <dd>
                        {typeof v === "string" && URL_RE.test(v) ? (
                          <ExternalLink href={v} className="break-all">
                            {v}
                          </ExternalLink>
                        ) : (
                          <InputValue v={v} />
                        )}
                      </dd>
                    </div>
                  ))}
                </dl>
              </Section>
            )}
            <Section title="Timeline">
              <Timeline events={events} />
            </Section>
          </>
        )}
      </div>
    </>
  );

  if (!sheet) {
    return (
      <aside ref={ref} className="tq-panel" aria-label="Task detail" data-status={current.status}>
        {body}
      </aside>
    );
  }
  return (
    <RadixDialog.Content
      asChild
      aria-describedby={undefined}
      // The sheet itself takes the focus, not its first control: that is the close button,
      // whose tooltip would open over the title.
      onOpenAutoFocus={(e) => {
        e.preventDefault();
        if (e.currentTarget instanceof HTMLElement) e.currentTarget.focus();
      }}
      // A press on a toast, or the expired session's card, leaves the sheet open.
      onPointerDownOutside={keepOpenOnToasts}
      // No Dialog.Trigger opened it, so Radix has nowhere to return the focus: the row has it back.
      onCloseAutoFocus={(e) => {
        const rowEl = document.querySelector<HTMLElement>(`[data-task-id="${row.task_id}"]`);
        if (!rowEl) return;
        e.preventDefault();
        rowEl.focus({ preventScroll: true });
      }}
    >
      <div ref={ref as Ref<HTMLDivElement>} className="tq-panel" data-sheet="" data-status={current.status}>
        {body}
      </div>
    </RadixDialog.Content>
  );
});
