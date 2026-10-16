import { useEffect, useRef, useState, type ReactNode } from "react";
import * as RadixDialog from "@radix-ui/react-dialog";
import { Button } from "@clickhouse/click-ui/Button";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { Icon } from "@clickhouse/click-ui/Icon";
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
  ACTION_LABEL,
  EVENT_LABEL,
  actionsFor,
  actorKind,
  actorName,
  briefSections,
  collapseHeartbeats,
  evidenceStatus,
  inputKind,
  issueHref,
  leaseMinutesLeft,
  posterKind,
  posterName,
  rel,
  resolvePolicy,
  shortId,
  tallyRecord,
  type PosterRecord,
  type TaskAction,
} from "../../lib/tasks";
import { humanizeError } from "../../lib/format";
import { toast } from "../../components/Toast";
import { IconButton } from "../../components/IconButton";
import { Tip, TipTarget } from "../../components/ui";
import { KindTag, PriorityBadge, StatusPill } from "./TaskBits";
import { MenuTrigger } from "../../components/ui/Menu";

/**
 * The detail panel: a document, then the verb. Header — status, kind, priority, title,
 * who handed it to which inbox. Body — for a triage task the poster's record and the
 * trust rule, then the brief as sections (Objective, Acceptance criteria, Inputs as
 * key/value with links, Constraints in a warning tone), the outcome, the reason a task
 * waits or ended, and the timeline from `task.history` with people, agents and the
 * sweeper told apart. Footer — only the actions the status allows, primary first; the
 * rest under one menu.
 *
 * Reads `task.get` + `task.history` once per opened task (and again after
 * an action, via `reloadKey`); a triage task adds one `task.list posted_by`
 * and one `inbox.policy_list`. Actions are the page's: the panel says which.
 */
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
        <a href={s} target="_blank" rel="noreferrer" className="break-all">
          {s.replace(/^https?:\/\//, "")} <Icon name="popout" size="xs" className="inline" aria-hidden="true" />
        </a>
      );
    case "issue": {
      const href = issueHref(s);
      return href ? (
        <a href={href} target="_blank" rel="noreferrer" className="font-mono text-xs">
          {s}
        </a>
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

// Each status allows at most one of these: the footer's primary, last on the right.
const PRIMARY_ACTIONS: { action: TaskAction; label: string; tip?: string }[] = [
  { action: "accept", label: "Accept" },
  { action: "start", label: "Start", tip: "Claim it: to do → in progress" },
  { action: "complete", label: "Mark done…" },
  { action: "unblock", label: "Unblock" },
  { action: "resume", label: "Resume now" },
];

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="tq-sec">
      <h3 className="section-label m-0 mb-1.5">{title}</h3>
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
      {record === undefined && <div className="text-xs text-text-subtle">counting…</div>}
      {record === null && <div className="text-xs text-text-subtle">not readable</div>}
      {record && (
        // biome-ignore lint/a11y/useSemanticElements: a labelled cluster of counts, not form fields
        <div
          className="tq-track"
          role="group"
          aria-label={`${record.total}${record.truncated ? "+" : ""} tasks from this poster: ${record.done} done, ${record.wont_do} won't do, ${record.failed} failed, ${record.open} open`}
        >
          {(
            [
              ["done", record.done],
              ["won't do", record.wont_do],
              ["failed", record.failed],
              ["open", record.open],
            ] as Array<[string, number]>
          ).map(([l, n]) => (
            <div key={l}>
              <div className="text-lg font-semibold leading-5 tabular">{n}</div>
              <div className="text-xs text-text-subtle">{l}</div>
            </div>
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

function Timeline({ events, now }: { events: TaskEvent[] | null; now: number }) {
  if (events === null) return <div className="text-xs text-text-muted">Loading…</div>;
  if (events.length === 0) return <div className="text-xs text-text-subtle">No events recorded.</div>;
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
              {kind === "human" ? "h" : kind === "sweeper" ? "s" : "b"}
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
                <span className="text-text-subtle">
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
            <span className="text-xs text-text-subtle whitespace-nowrap">
              <TipTarget tip={e.ts}>
                <time dateTime={e.ts}>{rel(e.ts, now)}</time>
              </TipTarget>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Menu({ items }: { items: Array<{ label: string; onClick: () => void; disabled?: boolean }> }) {
  return (
    <Dropdown modal={false}>
      <MenuTrigger>
        <IconButton icon="dots-horizontal" label="More actions" />
      </MenuTrigger>
      <Dropdown.Content align="end">
        {items.map((it) => (
          <Dropdown.Item key={it.label} disabled={it.disabled} onSelect={it.onClick}>
            {it.label}
          </Dropdown.Item>
        ))}
      </Dropdown.Content>
    </Dropdown>
  );
}

export function TaskPanel({
  identity,
  row,
  inboxes,
  canWrite,
  now,
  reloadKey,
  onClose,
  onAction,
  onOpenTask,
  onHistory,
  sheet = false,
}: Props) {
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
          setError(humanizeError(e));
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

  const current: TaskRow | TaskListRow = task ?? row;
  const inbox = inboxes.find((i) => i.inbox === current.inbox);
  const handed = posterKind(current, inbox?.owner_agent) !== "me";
  const allowed = canWrite ? new Set(actionsFor(current.status)) : new Set<TaskAction>();
  const brief = briefSections(task?.brief);
  const outcome = task?.outcome && Object.keys(task.outcome).length > 0 ? task.outcome : null;
  const act = (a: TaskAction) => onAction(a, a === "edit" && task ? task : current);
  const primary = PRIMARY_ACTIONS.find((p) => allowed.has(p.action));
  const copyId = () => {
    navigator.clipboard
      ?.writeText(current.task_id)
      .then(() => toast("Task id copied", "ok"))
      .catch(() => toast(current.task_id, "ok"));
  };

  const menu: Array<{ label: string; onClick: () => void; disabled?: boolean }> = [];
  if (allowed.has("edit")) menu.push({ label: ACTION_LABEL.edit, onClick: () => act("edit"), disabled: !task });
  if (allowed.has("reassign")) menu.push({ label: `${ACTION_LABEL.reassign}…`, onClick: () => act("reassign") });
  if (allowed.has("cancel")) menu.push({ label: `${ACTION_LABEL.cancel}…`, onClick: () => act("cancel") });
  menu.push({ label: "Copy id", onClick: copyId });

  const lease = current.status === "in_progress" ? leaseMinutesLeft(current.lease_until, now) : null;
  const reasonTitle =
    current.status === "blocked"
      ? `Blocked on ${current.blocked_on || "a person"}`
      : current.status === "deferred"
        ? `Deferred${current.resume_at ? ` · resumes ${rel(current.resume_at, now)}` : ""}`
        : "Reason";
  const parentTaskId = current.parent_task_id;

  const titleClass = "text-lg font-semibold m-0 mb-1 [text-wrap:balance] break-words";
  const body = (
    <>
      <div className="px-3.5 pt-3 pb-2.5 border-b border-border">
        <div className="flex items-center gap-2 flex-wrap mb-2">
          <StatusPill status={current.status} />
          <KindTag kind={current.kind} />
          <PriorityBadge priority={current.priority} />
          <span className="flex-1" />
          <IconButton icon="cross" label="Close" onClick={onClose} />
        </div>
        {sheet ? (
          <RadixDialog.Title className={titleClass}>{current.title}</RadixDialog.Title>
        ) : (
          <h2 className={titleClass}>{current.title}</h2>
        )}
        <div className="text-xs text-text-subtle flex gap-1.5 items-center flex-wrap">
          {handed ? (
            <>
              <b className="font-medium text-text-muted">from {posterName(current)}</b>
              <span>→ {current.inbox}</span>
            </>
          ) : (
            <b className="font-medium text-text-muted">this agent's own task</b>
          )}
          <span>· posted {rel(current.created_at, now)}</span>
          <span>
            ·{" "}
            <TipTarget tip={current.task_id}>
              <span className="font-mono">{shortId(current.task_id)}</span>
            </TipTarget>
          </span>
          {parentTaskId && (
            <Button type="empty" htmlType="button" iconLeft="git-merge" onClick={() => onOpenTask(parentTaskId)}>
              Reassigned from {shortId(parentTaskId)}
            </Button>
          )}
        </div>
      </div>

      <div className="overflow-y-auto px-3.5 pt-1 pb-3.5 flex-1 min-h-0">
        {error && <div className="text-xs text-danger py-2">{error}</div>}
        {task?.status === "triage" && <Track record={record} policy={policy} task={task} />}
        <Section title="Objective">
          {task ? (
            <p className="m-0 text-text-muted whitespace-pre-wrap">
              {brief.objective || <span className="text-text-subtle">The poster left the objective empty.</span>}
            </p>
          ) : (
            <div className="text-xs text-text-muted">Loading…</div>
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
                      <a href={v} target="_blank" rel="noreferrer" className="break-all">
                        {v}
                      </a>
                    ) : (
                      <InputValue v={v} />
                    )}
                  </dd>
                </div>
              ))}
            </dl>
          </Section>
        )}
        {current.reason &&
          (current.status === "blocked" ||
            current.status === "deferred" ||
            current.status === "failed" ||
            current.status === "wont_do" ||
            current.status === "cancelled") && (
            <Section title={reasonTitle}>
              <p className="m-0 text-text-muted whitespace-pre-wrap">{current.reason}</p>
            </Section>
          )}
        <Section title="Timeline">
          <Timeline events={events} now={now} />
        </Section>
      </div>

      {/* biome-ignore lint/a11y/useSemanticElements: a labelled cluster of action buttons, not form fields */}
      <div
        className="border-t border-border px-3.5 py-2.5 flex gap-2 items-center justify-end flex-wrap"
        role="group"
        aria-label="Actions"
      >
        {current.status === "deferred" && current.resume_at && (
          <span className="text-xs text-text-subtle">otherwise resumes {rel(current.resume_at, now)}</span>
        )}
        {current.status === "in_progress" && (
          <span className="text-xs text-text-subtle">
            owned by <b className="font-medium text-text-muted">{current.owner || "—"}</b>
            {lease !== null && <> · lease ends in {lease} min</>}
          </span>
        )}
        <span className="flex-1" />
        <Menu items={menu} />
        {allowed.has("fail") && (
          <Button type="secondary" htmlType="button" onClick={() => act("fail")}>
            Fail…
          </Button>
        )}
        {allowed.has("defer") && (
          <Button type="secondary" htmlType="button" onClick={() => act("defer")}>
            Defer…
          </Button>
        )}
        {allowed.has("block") && (
          <Button type="secondary" htmlType="button" onClick={() => act("block")}>
            Block…
          </Button>
        )}
        {allowed.has("wont_do") && (
          <Button type="secondary" htmlType="button" onClick={() => act("wont_do")}>
            Won't do…
          </Button>
        )}
        {allowed.has("reopen") && (
          <Button type="secondary" htmlType="button" onClick={() => act("reopen")}>
            Reopen…
          </Button>
        )}
        {primary && (
          <Tip content={primary.tip} disabled={!primary.tip}>
            <Button type="primary" htmlType="button" onClick={() => act(primary.action)}>
              {primary.label}
            </Button>
          </Tip>
        )}
      </div>
    </>
  );

  if (!sheet) {
    return (
      <aside className="tq-panel" aria-label="Task detail" data-status={current.status}>
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
      // No Dialog.Trigger opened it, so Radix has nowhere to return the focus: the row has it back.
      onCloseAutoFocus={(e) => {
        const rowEl = document.querySelector<HTMLElement>(`[data-task-id="${row.task_id}"]`);
        if (!rowEl) return;
        e.preventDefault();
        rowEl.focus({ preventScroll: true });
      }}
    >
      <div className="tq-panel outline-none" data-sheet="" data-status={current.status}>
        {body}
      </div>
    </RadixDialog.Content>
  );
}
