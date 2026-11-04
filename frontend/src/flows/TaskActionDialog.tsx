import { useEffect, useState } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Select } from "@clickhouse/click-ui/Select";
import { TextAreaField } from "@clickhouse/click-ui/TextAreaField";
import { TextField } from "@clickhouse/click-ui/TextField";
import {
  acceptTask,
  blockTask,
  cancelTask,
  completeTask,
  deferTask,
  failTask,
  getTask,
  listInboxes,
  reassignTask,
  reopenTask,
  unblockTask,
  wontDoTask,
  type BlockedOn,
  type Caller,
  type Inbox,
  type TaskListRow,
  type TaskRow,
} from "../api/tasks";
import { ACTION_LABEL, PRIORITY_LABEL, actionDone, isoFromLocal, type TaskAction } from "../lib/tasks";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import { CancelButton, DateTimeField, DialogFrame, ErrorNote } from "./TaskDialogFrame";
import { oneOf } from "../lib/oneOf";

/**
 * One transition of a task, with its reason (every move off the happy path carries one).
 * Accept takes an optional priority edit; defer a resume time; block what it is blocked
 * on; reassign the inbox the task goes to. Unblock takes a note. The others take the
 * reason alone.
 */
export type ReasonAction = Exclude<TaskAction, "edit" | "resume" | "start">;

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  identity: Caller;
  action: ReasonAction;
  task: TaskListRow | TaskRow;
  /** For reassign: a first list of inboxes the task may move to; the dialog
   *  then asks `inbox.list mine_everywhere` for every inbox the caller may
   *  post into — the ones a reassign can reach. */
  inboxes?: Inbox[];
  onDone: (task: TaskRow, replacement?: TaskRow) => void;
  /** Where the focus goes as the dialog closes; without it, back to the control that opened it. */
  onCloseAutoFocus?: (e: Event) => void;
}

const BLOCKED_ON: ReadonlyArray<{ value: BlockedOn; label: string }> = [
  { value: "human", label: "a person" },
  { value: "sender", label: "the sender" },
  { value: "external", label: "an external event" },
];

const DESCRIPTION: Record<ReasonAction, string> = {
  accept: "Moves the task from triage to the queue.",
  complete: "Closes the task as done and releases its lease.",
  fail: "Closes the task as failed and releases its lease; it can be reopened.",
  wont_do: "Closes the task without doing it.",
  defer: "Parks the task until the resume time; after the inbox's defer limit it is blocked on a person instead.",
  block: "Takes the task out of the queue until whoever it waits on unblocks it.",
  unblock: "Returns the task to the queue.",
  cancel: "Closes the task; a task in progress loses its lease.",
  reassign:
    "Cancels the task here and posts a copy into another inbox, whose trust policy applies to the original poster.",
  reopen: "Puts a failed, won't-do or cancelled task back in the queue.",
};

const NEEDS_REASON: ReadonlySet<ReasonAction> = new Set([
  "fail",
  "wont_do",
  "defer",
  "block",
  "cancel",
  "reassign",
  "reopen",
]);
// The danger button for a move that throws work away. Won't do is a decision with a reason,
// undone by Reopen: the primary.
const DESTRUCTIVE: ReadonlySet<ReasonAction> = new Set(["fail", "cancel"]);
/** The owner writes a task in progress must fence with its current lease. */
const FENCED: ReadonlySet<ReasonAction> = new Set(["complete", "fail", "defer", "block"]);

export function TaskActionDialog({
  open,
  onOpenChange,
  identity,
  action,
  task,
  inboxes = [],
  onDone,
  onCloseAutoFocus,
}: Props) {
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [priority, setPriority] = useState<number>(task.priority);
  const [resumeAt, setResumeAt] = useState("");
  const [blockedOn, setBlockedOn] = useState<BlockedOn>("human");
  const [reachable, setReachable] = useState<Inbox[]>(inboxes);
  useEffect(() => {
    if (action !== "reassign") return;
    listInboxes(identity, { mineEverywhere: true })
      .then((list) =>
        setReachable((cur) => {
          const seen = new Set(list.map((i) => i.inbox));
          return [...list, ...cur.filter((i) => !seen.has(i.inbox))];
        }),
      )
      .catch(() => {
        /* the first list stands */
      });
  }, [action, identity]);
  const targets = reachable.filter((i) => i.inbox !== task.inbox);
  const [toInbox, setToInbox] = useState(targets[0]?.inbox ?? "");
  useEffect(() => {
    if (!toInbox && targets[0]) setToInbox(targets[0].inbox);
  }, [targets, toInbox]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit =
    !busy && (!NEEDS_REASON.has(action) || reason.trim() !== "") && (action !== "reassign" || toInbox !== "");
  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    const id = task.task_id;
    const r = reason.trim();
    try {
      // The list row's fence may be a lease behind; read the task's own. Won't do and cancel
      // carry none: the handler treats a person's as the owner side's and drops the lease.
      const fence =
        task.status === "in_progress" && FENCED.has(action) ? ((await getTask(identity, id))?.fence ?? 0) : 0;
      let result: { task: TaskRow; replacement?: TaskRow };
      switch (action) {
        case "accept":
          result = await acceptTask(identity, id, {
            ...(priority !== task.priority ? { priority } : {}),
            ...(note.trim() ? { note: note.trim() } : {}),
          });
          break;
        case "complete":
          result = await completeTask(identity, id, fence, note.trim());
          break;
        case "fail":
          result = await failTask(identity, id, fence, r);
          break;
        case "wont_do":
          result = await wontDoTask(identity, id, r);
          break;
        case "defer":
          result = await deferTask(identity, id, r, isoFromLocal(resumeAt), fence);
          break;
        case "block":
          result = await blockTask(identity, id, r, blockedOn, fence);
          break;
        case "unblock":
          result = await unblockTask(identity, id, note.trim());
          break;
        case "cancel":
          result = await cancelTask(identity, id, r);
          break;
        case "reopen":
          result = await reopenTask(identity, id, r);
          break;
        case "reassign":
          result = await reassignTask(identity, id, toInbox, r);
          break;
      }
      toast(actionDone(action, task.title), "ok");
      onDone(result.task, result.replacement);
      onOpenChange(false);
    } catch (e) {
      setError(humanizeError(e));
      setBusy(false);
    }
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      title={ACTION_LABEL[action]}
      description={DESCRIPTION[action]}
      onCloseAutoFocus={onCloseAutoFocus}
      footer={
        <>
          <CancelButton label="Back" />
          <Button
            type={DESTRUCTIVE.has(action) ? "danger" : "primary"}
            htmlType="button"
            onClick={submit}
            disabled={!canSubmit}
            loading={busy}
          >
            {ACTION_LABEL[action]}
          </Button>
        </>
      }
    >
      <p className="text-sm font-medium text-text">“{task.title}”</p>
      {action === "accept" && (
        <Select
          label="Priority (edit while accepting, optional)"
          value={String(priority)}
          onSelect={(v) => setPriority(Number(v))}
        >
          {[1, 2, 3, 4].map((p) => (
            <Select.Item key={p} value={String(p)}>
              {PRIORITY_LABEL[p]}
            </Select.Item>
          ))}
        </Select>
      )}
      {action === "reassign" &&
        (targets.length === 0 ? (
          <p className="text-xs text-text-muted">No other inbox you may post into.</p>
        ) : (
          <Select label="To inbox" value={toInbox} onSelect={setToInbox}>
            {targets.map((i) => (
              <Select.Item key={i.inbox} value={i.inbox}>
                {i.inbox} — {i.owner_agent} ({i.owner_namespace})
              </Select.Item>
            ))}
          </Select>
        ))}
      {action === "block" && (
        <Select
          label="Blocked on"
          value={blockedOn}
          onSelect={(value) => {
            const v = oneOf(
              BLOCKED_ON.map((o) => o.value),
              value,
            );
            if (v !== null) setBlockedOn(v);
          }}
        >
          {BLOCKED_ON.map((o) => (
            <Select.Item key={o.value} value={o.value}>
              {o.label}
            </Select.Item>
          ))}
        </Select>
      )}
      {action === "defer" && (
        <DateTimeField
          id="task-defer-resume"
          label="Resume at (the sweeper returns it to the queue then)"
          value={resumeAt}
          onChange={setResumeAt}
        />
      )}
      {NEEDS_REASON.has(action) && (
        <TextAreaField
          id="task-action-reason"
          label="Reason (required; shown on the card and in the feed)"
          value={reason}
          onChange={setReason}
          rows={3}
          autoFocus
        />
      )}
      {(action === "accept" || action === "unblock" || action === "complete") && (
        <TextField id="task-action-note" label="Note (optional)" value={note} onChange={setNote} autoFocus />
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}
