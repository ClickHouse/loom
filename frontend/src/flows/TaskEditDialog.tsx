import { useMemo, useState } from "react";
import { Pencil, Plus } from "lucide-react";
import { createTask, updateTask, type Caller, type Disclosure, type Inbox, type TaskRow } from "../api/tasks";
import { PRIORITY_LABEL, conflictOf, isoFromLocal, localFromIso, type UpdateConflict } from "../lib/tasks";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import {
  CancelButton,
  DialogFrame,
  ErrorNote,
  Field,
  INPUT_CLS,
  PRIMARY_BTN,
  SECONDARY_BTN,
  TEXTAREA_CLS,
} from "./TaskDialogFrame";
import { oneOf } from "../lib/oneOf";

/**
 * Create a task in an inbox, or edit one under optimistic concurrency.
 *
 * The brief is the task's own text: an `objective`, `acceptance_criteria`
 * (one per line) and whatever else the poster put there — an edit keeps the
 * keys it does not show. An edit sends `if_match_version`; when the row
 * moved since it was read, the server answers 409 PRECONDITION_FAILED with
 * the current row, and the dialog shows that row's title and brief beside
 * the draft so the person can decide, then retries with the new version.
 */
export type TaskEditMode = { kind: "create"; inbox: string; inboxes?: Inbox[] } | { kind: "edit"; task: TaskRow };

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Caller;
  mode: TaskEditMode;
  onSaved: (task: TaskRow) => void;
}

const DISCLOSURES: Disclosure[] = ["confidential", "internal", "public_ok"];

function criteriaOf(brief: Record<string, unknown> | undefined): string {
  const v = brief?.acceptance_criteria;
  if (Array.isArray(v)) return v.map(String).join("\n");
  return typeof v === "string" ? v : "";
}

function objectiveOf(brief: Record<string, unknown> | undefined): string {
  const v = brief?.objective;
  return typeof v === "string" ? v : v == null ? "" : JSON.stringify(v);
}

export function TaskEditDialog({ open, onOpenChange, identity, mode, onSaved }: Props) {
  const editing = mode.kind === "edit" ? mode.task : null;
  const [inbox, setInbox] = useState(mode.kind === "create" ? mode.inbox : "");
  const [kind, setKind] = useState(editing?.kind ?? "");
  const [title, setTitle] = useState(editing?.title ?? "");
  const [objective, setObjective] = useState(objectiveOf(editing?.brief));
  const [criteria, setCriteria] = useState(criteriaOf(editing?.brief));
  const [priority, setPriority] = useState<number>(editing?.priority ?? 3);
  const [disclosure, setDisclosure] = useState<Disclosure>(editing?.disclosure ?? "internal");
  const [notBefore, setNotBefore] = useState(localFromIso(editing?.not_before));
  const [deadline, setDeadline] = useState(localFromIso(editing?.deadline));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The version the next task.update must match; moves forward on a 409
  // once the person chooses to retry over the newer row.
  const [version, setVersion] = useState(editing?.version ?? 0);
  const [conflict, setConflict] = useState<UpdateConflict | null>(null);

  const inboxes = mode.kind === "create" ? (mode.inboxes ?? []) : [];
  const canSubmit = title.trim() !== "" && (editing ? true : kind.trim() !== "" && inbox !== "") && !busy;

  const brief = useMemo(() => {
    const base: Record<string, unknown> = { ...(editing?.brief ?? {}) };
    if (objective.trim()) base.objective = objective.trim();
    else delete base.objective;
    const lines = criteria
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    if (lines.length) base.acceptance_criteria = lines;
    else delete base.acceptance_criteria;
    return base;
  }, [editing, objective, criteria]);

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    setConflict(null);
    try {
      if (editing) {
        const r = await updateTask(identity, {
          task_id: editing.task_id,
          if_match_version: version,
          title: title.trim(),
          brief,
          priority,
          ...(deadline ? { deadline: isoFromLocal(deadline) } : editing.deadline ? { clear_deadline: true } : {}),
          ...(notBefore
            ? { not_before: isoFromLocal(notBefore) }
            : editing.not_before
              ? { clear_not_before: true }
              : {}),
          ...(note.trim() ? { note: note.trim() } : {}),
        });
        toast("Task updated", "ok");
        onSaved(r.task);
      } else {
        const r = await createTask(identity, {
          inbox,
          kind: kind.trim(),
          title: title.trim(),
          brief,
          priority,
          disclosure,
          not_before: isoFromLocal(notBefore),
          deadline: isoFromLocal(deadline),
        });
        toast(r.task.status === "triage" ? "Task posted — waiting in triage" : "Task posted", "ok");
        onSaved(r.task);
      }
      onOpenChange(false);
    } catch (e) {
      const c = conflictOf(e);
      if (c) {
        setConflict(c);
        setError("Someone changed this task while you were editing. Compare and retry over their version, or close.");
      } else {
        setError(humanizeError(e));
      }
      setBusy(false);
    }
  };

  const retryOverCurrent = () => {
    if (!conflict) return;
    setVersion(conflict.currentVersion);
    setConflict(null);
    setError(null);
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? "Edit task" : "New task"}
      icon={editing ? <Pencil size={16} /> : <Plus size={16} />}
      description={
        editing
          ? "Changes the brief; the task keeps its status and history."
          : "Posts into the inbox; the inbox's trust policy decides whether it waits in triage or is ready to pull."
      }
      width={620}
      footer={
        <>
          <CancelButton />
          {conflict && (
            <button type="button" onClick={retryOverCurrent} className={SECONDARY_BTN}>
              Retry over version {conflict.currentVersion}
            </button>
          )}
          <button type="button" onClick={submit} disabled={!canSubmit || !!conflict} className={PRIMARY_BTN}>
            {busy ? "Saving…" : editing ? "Save changes" : "Post task"}
          </button>
        </>
      }
    >
      {!editing && inboxes.length > 1 && (
        <Field id="task-inbox" label="Inbox">
          <select id="task-inbox" value={inbox} onChange={(e) => setInbox(e.target.value)} className={INPUT_CLS}>
            {inboxes.map((i) => (
              <option key={i.inbox} value={i.inbox}>
                {i.inbox} — {i.owner_agent}
              </option>
            ))}
          </select>
        </Field>
      )}
      {!editing && (
        <Field id="task-kind" label="Kind" hint="what sort of work; the trust table is per kind">
          <input
            id="task-kind"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            placeholder="bug_report"
            className={`${INPUT_CLS} font-mono`}
            autoFocus
          />
        </Field>
      )}
      <Field id="task-title" label="Title">
        <input
          id="task-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="One line the receiver reads first"
          className={INPUT_CLS}
          autoFocus={!!editing}
        />
      </Field>
      <Field id="task-objective" label="Objective" hint="brief.objective">
        <textarea
          id="task-objective"
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          rows={3}
          className={TEXTAREA_CLS}
          placeholder="What done looks like, in the receiver's terms."
        />
      </Field>
      <Field id="task-criteria" label="Acceptance criteria" hint="one per line">
        <textarea
          id="task-criteria"
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
          rows={3}
          className={TEXTAREA_CLS}
          placeholder={"A test covers the regression\nThe fix is on the release branch"}
        />
      </Field>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field id="task-priority" label="Priority">
          <select
            id="task-priority"
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
            className={INPUT_CLS}
          >
            {[1, 2, 3, 4].map((p) => (
              <option key={p} value={p}>
                {PRIORITY_LABEL[p]}
              </option>
            ))}
          </select>
        </Field>
        {!editing && (
          <Field id="task-disclosure" label="Disclosure" hint="the inbox must accept it">
            <select
              id="task-disclosure"
              value={disclosure}
              onChange={(e) => {
                const d = oneOf(DISCLOSURES, e.target.value);
                if (d !== null) setDisclosure(d);
              }}
              className={INPUT_CLS}
            >
              {DISCLOSURES.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Field id="task-not-before" label="Not before" hint="hidden from pull until then">
          <input
            id="task-not-before"
            type="datetime-local"
            value={notBefore}
            onChange={(e) => setNotBefore(e.target.value)}
            className={INPUT_CLS}
          />
        </Field>
        <Field id="task-deadline" label="Deadline">
          <input
            id="task-deadline"
            type="datetime-local"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
            className={INPUT_CLS}
          />
        </Field>
      </div>
      {editing && (
        <Field id="task-note" label="Note" hint="optional, goes in the activity feed">
          <input
            id="task-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className={INPUT_CLS}
            placeholder="Why this changed"
          />
        </Field>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {conflict && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs" aria-label="Conflict comparison">
          <div className="rounded-lg border border-[var(--color-border)] p-3 space-y-1.5">
            <div className="section-label">Your edit</div>
            <div className="font-medium break-words">{title}</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-2xs text-[var(--color-text-muted)]">
              {JSON.stringify(brief, null, 1)}
            </pre>
          </div>
          <div className="rounded-lg border border-[var(--color-warn)]/40 p-3 space-y-1.5">
            <div className="section-label">Current (version {conflict.currentVersion})</div>
            <div className="font-medium break-words">{conflict.current?.title ?? "—"}</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-2xs text-[var(--color-text-muted)]">
              {JSON.stringify(conflict.current?.brief ?? {}, null, 1)}
            </pre>
            {conflict.current && (
              <div className="text-[var(--color-text-subtle)]">
                status {conflict.current.status} · last actor {conflict.current.last_actor || "—"}
              </div>
            )}
          </div>
        </div>
      )}
    </DialogFrame>
  );
}
