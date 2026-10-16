import { useEffect, useRef, useState, type ReactNode } from "react";
import { ExternalLink, GitBranch, MoreHorizontal, X as XIcon } from "lucide-react";
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
import { KindTag, PriorityBadge, StatusPill, TqButton } from "./TaskBits";

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
}

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
    return <pre className="text-2xs font-mono whitespace-pre-wrap break-all m-0">{JSON.stringify(v, null, 1)}</pre>;
  const s = String(v);
  switch (inputKind(s)) {
    case "url":
      return (
        <a href={s} target="_blank" rel="noreferrer" className="break-all">
          {s.replace(/^https?:\/\//, "")} <ExternalLink size={10} className="inline" aria-hidden="true" />
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

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="tq-sec">
      <h4>{title}</h4>
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
      {record === undefined && <div className="text-xs text-[var(--color-text-subtle)]">counting…</div>}
      {record === null && <div className="text-xs text-[var(--color-text-subtle)]">not readable</div>}
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
              <div className="text-2xs text-[var(--color-text-subtle)]">{l}</div>
            </div>
          ))}
        </div>
      )}
      <p className="text-xs text-[var(--color-text-muted)] mt-1.5 m-0">
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
  if (events === null) return <div className="text-xs text-[var(--color-text-muted)]">Loading…</div>;
  if (events.length === 0) return <div className="text-xs text-[var(--color-text-subtle)]">No events recorded.</div>;
  const asc = [...events].sort((a, b) => a.seq - b.seq);
  const rows = collapseHeartbeats(asc).reverse();
  return (
    <ul className="tq-tl">
      {rows.map((e) => {
        const kind = actorKind(e.actor, e.agent);
        const text = e.reason || e.note;
        return (
          <li key={e.seq}>
            <span
              className="ic"
              data-kind={kind}
              title={kind === "human" ? "a person" : kind === "sweeper" ? "the lease sweeper" : "an agent"}
              role="img"
              aria-label={kind}
            >
              {kind === "human" ? "h" : kind === "sweeper" ? "s" : "b"}
            </span>
            <span className="text-[var(--color-text-muted)] min-w-0">
              <b className="font-medium text-[var(--color-text)]">
                {e.collapsed && e.collapsed > 1
                  ? `${e.collapsed} heartbeats`
                  : (EVENT_LABEL[e.event] ?? e.event.replace(/_/g, " "))}
              </b>
              {" · "}
              {actorName(e.actor, e.agent)}
              {e.from_status && e.to_status && e.from_status !== e.to_status && (
                <span className="text-[var(--color-text-subtle)]">
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
            <span className="text-xs text-[var(--color-text-subtle)] whitespace-nowrap" title={e.ts}>
              {rel(e.ts, now)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function Menu({ items }: { items: Array<{ label: string; onClick: () => void; disabled?: boolean }> }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target instanceof Node ? e.target : null;
      if (!ref.current?.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <TqButton
        tone="ghost"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="px-2"
      >
        <MoreHorizontal size={16} aria-hidden="true" />
      </TqButton>
      {open && (
        <div className="tq-menu" role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              disabled={it.disabled}
              onClick={() => {
                setOpen(false);
                it.onClick();
              }}
              className="disabled:opacity-50"
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
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

  return (
    <aside className="tq-panel" aria-label="Task detail" data-status={current.status}>
      <div className="px-3.5 pt-3 pb-2.5 border-b border-[var(--color-border)]">
        <div className="flex items-center gap-2 flex-wrap mb-2">
          <StatusPill status={current.status} />
          <KindTag kind={current.kind} />
          <PriorityBadge priority={current.priority} />
          <span className="flex-1" />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="p-1.5 -mr-1.5 rounded-md text-[var(--color-text-muted)] hover:text-[var(--color-text)] hover:bg-[var(--color-surface-2)]"
          >
            <XIcon size={16} />
          </button>
        </div>
        <h2 className="text-lg font-semibold leading-[22px] m-0 mb-1 [text-wrap:balance] break-words">
          {current.title}
        </h2>
        <div className="text-xs text-[var(--color-text-subtle)] flex gap-1.5 items-center flex-wrap">
          {handed ? (
            <>
              <b className="font-medium text-[var(--color-text-muted)]">from {posterName(current)}</b>
              <span>→ {current.inbox}</span>
            </>
          ) : (
            <b className="font-medium text-[var(--color-text-muted)]">this agent's own task</b>
          )}
          <span>· posted {rel(current.created_at, now)}</span>
          <span className="font-mono" title={current.task_id}>
            · {shortId(current.task_id)}
          </span>
          {parentTaskId && (
            <button
              type="button"
              onClick={() => onOpenTask(parentTaskId)}
              className="inline-flex items-center gap-1 underline font-mono"
            >
              <GitBranch size={10} aria-hidden="true" /> reassigned from {shortId(parentTaskId)}
            </button>
          )}
        </div>
      </div>

      <div className="overflow-y-auto px-3.5 pt-1 pb-3.5 flex-1 min-h-0">
        {error && <div className="text-xs text-danger py-2">{error}</div>}
        {task?.status === "triage" && <Track record={record} policy={policy} task={task} />}
        <Section title="Objective">
          {task ? (
            <p className="m-0 text-[var(--color-text-muted)] whitespace-pre-wrap">
              {brief.objective || (
                <span className="text-[var(--color-text-subtle)]">The poster left the objective empty.</span>
              )}
            </p>
          ) : (
            <div className="text-xs text-[var(--color-text-muted)]">Loading…</div>
          )}
        </Section>
        {brief.criteria.length > 0 && (
          <Section title="Acceptance criteria">
            <ul className="m-0 pl-[18px] text-[var(--color-text-muted)] list-disc">
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
              <p className="m-0 text-[var(--color-text-muted)] whitespace-pre-wrap">{current.reason}</p>
            </Section>
          )}
        <Section title="Timeline">
          <Timeline events={events} now={now} />
        </Section>
      </div>

      {/* biome-ignore lint/a11y/useSemanticElements: a labelled cluster of action buttons, not form fields */}
      <div
        className="border-t border-[var(--color-border)] px-3.5 py-2.5 flex gap-2 items-center flex-wrap"
        role="group"
        aria-label="Actions"
      >
        {allowed.has("accept") && (
          <TqButton tone="primary" onClick={() => act("accept")}>
            Accept
          </TqButton>
        )}
        {allowed.has("start") && (
          <TqButton tone="primary" onClick={() => act("start")} title="Claim it: to do → in progress">
            Start
          </TqButton>
        )}
        {allowed.has("complete") && (
          <TqButton tone="primary" onClick={() => act("complete")}>
            Mark done…
          </TqButton>
        )}
        {allowed.has("fail") && <TqButton onClick={() => act("fail")}>Fail…</TqButton>}
        {allowed.has("unblock") && (
          <TqButton tone="primary" onClick={() => act("unblock")}>
            Unblock
          </TqButton>
        )}
        {allowed.has("resume") && (
          <TqButton tone="primary" onClick={() => act("resume")}>
            Resume now
          </TqButton>
        )}
        {allowed.has("defer") && <TqButton onClick={() => act("defer")}>Defer…</TqButton>}
        {allowed.has("block") && <TqButton onClick={() => act("block")}>Block…</TqButton>}
        {allowed.has("wont_do") && <TqButton onClick={() => act("wont_do")}>Won't do…</TqButton>}
        {allowed.has("reopen") && <TqButton onClick={() => act("reopen")}>Reopen…</TqButton>}
        {current.status === "deferred" && current.resume_at && (
          <span className="text-xs text-[var(--color-text-subtle)]">
            otherwise resumes {rel(current.resume_at, now)}
          </span>
        )}
        {current.status === "in_progress" && (
          <span className="text-xs text-[var(--color-text-subtle)]">
            owned by <b className="font-medium text-[var(--color-text-muted)]">{current.owner || "—"}</b>
            {lease !== null && <> · lease ends in {lease} min</>}
          </span>
        )}
        <span className="flex-1" />
        <Menu items={menu} />
      </div>
    </aside>
  );
}
