import { useMemo, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import { createTask, updateTask, type Caller, type Disclosure, type Inbox, type TaskRow } from "../api/tasks";
import { PRIORITY_LABEL, conflictOf, isoFromLocal, localFromIso, type UpdateConflict } from "../lib/tasks";
import { humanizeError } from "../lib/errorWords";
import { toast } from "../components/Toast";
import { CancelButton, DateTimeField, DialogFrame, ErrorNote } from "./TaskDialogFrame";
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
            <Button type="secondary" htmlType="button" onClick={retryOverCurrent}>
              Retry over version {conflict.currentVersion}
            </Button>
          )}
          <Button type="primary" htmlType="button" onClick={submit} disabled={!canSubmit || !!conflict} loading={busy}>
            {editing ? "Save changes" : "Post task"}
          </Button>
        </>
      }
    >
      {!editing && inboxes.length > 1 && (
        <Select label="Inbox" value={inbox} onSelect={setInbox}>
          {inboxes.map((i) => (
            <Select.Item key={i.inbox} value={i.inbox}>
              {i.inbox} — {i.owner_agent}
            </Select.Item>
          ))}
        </Select>
      )}
      {!editing && (
        <TextField
          id="task-kind"
          label="Kind (what sort of work; the trust table is per kind)"
          value={kind}
          onChange={setKind}
          placeholder="bug_report"
          className="font-mono"
          autoFocus
        />
      )}
      <TextField
        id="task-title"
        label="Title"
        value={title}
        onChange={setTitle}
        placeholder="One line the receiver reads first"
        autoFocus={!!editing}
      />
      <TextAreaField
        id="task-objective"
        label="Objective (brief.objective)"
        value={objective}
        onChange={setObjective}
        rows={3}
        placeholder="What done looks like, in the receiver's terms."
      />
      <TextAreaField
        id="task-criteria"
        label="Acceptance criteria (one per line)"
        value={criteria}
        onChange={setCriteria}
        rows={3}
        placeholder={"A test covers the regression\nThe fix is on the release branch"}
      />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Select label="Priority" value={String(priority)} onSelect={(v) => setPriority(Number(v))}>
          {[1, 2, 3, 4].map((p) => (
            <Select.Item key={p} value={String(p)}>
              {PRIORITY_LABEL[p]}
            </Select.Item>
          ))}
        </Select>
        {!editing && (
          <Select
            label="Disclosure (the inbox must accept it)"
            value={disclosure}
            onSelect={(value) => {
              const d = oneOf(DISCLOSURES, value);
              if (d !== null) setDisclosure(d);
            }}
          >
            {DISCLOSURES.map((d) => (
              <Select.Item key={d} value={d}>
                {d}
              </Select.Item>
            ))}
          </Select>
        )}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <DateTimeField
          id="task-not-before"
          label="Not before (hidden from pull until then)"
          value={notBefore}
          onChange={setNotBefore}
        />
        <DateTimeField id="task-deadline" label="Deadline" value={deadline} onChange={setDeadline} />
      </div>
      {editing && (
        <TextField
          id="task-note"
          label="Note (optional, goes in the activity feed)"
          value={note}
          onChange={setNote}
          placeholder="Why this changed"
        />
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
      {conflict && (
        // biome-ignore lint/a11y/useSemanticElements: a read-only side-by-side comparison, not form controls
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs" role="group" aria-label="Conflict comparison">
          <div className="rounded-lg border border-border p-3 space-y-1.5">
            <div className="section-label">Your edit</div>
            <div className="font-medium break-words">{title}</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-xs text-text-muted">
              {JSON.stringify(brief, null, 1)}
            </pre>
          </div>
          <div className="rounded-lg border border-warn/40 p-3 space-y-1.5">
            <div className="section-label">Current (version {conflict.currentVersion})</div>
            <div className="font-medium break-words">{conflict.current?.title ?? "—"}</div>
            <pre className="whitespace-pre-wrap break-words font-mono text-xs text-text-muted">
              {JSON.stringify(conflict.current?.brief ?? {}, null, 1)}
            </pre>
            {conflict.current && (
              <div className="text-text-muted">
                status {conflict.current.status} · last actor {conflict.current.last_actor || "—"}
              </div>
            )}
          </div>
        </div>
      )}
    </DialogFrame>
  );
}
