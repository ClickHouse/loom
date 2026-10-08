import { useEffect, useState } from "react";
import {
  ArrowLeftRight,
  Ban,
  Check,
  CheckCircle2,
  Clock,
  Lock,
  LockOpen,
  RotateCcw,
  XCircle,
  XOctagon,
} from "lucide-react";
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
import { ACTION_LABEL, PRIORITY_LABEL, isoFromLocal, type TaskAction } from "../lib/tasks";
import { humanizeError } from "../lib/format";
import { toast } from "../components/Toast";
import {
  CancelButton,
  DANGER_BTN,
  DialogFrame,
  ErrorNote,
  Field,
  INPUT_CLS,
  PRIMARY_BTN,
  TEXTAREA_CLS,
} from "./TaskDialogFrame";
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
   *  read or post into — the ones a blind post can reach. */
  inboxes?: Inbox[];
  onDone: (task: TaskRow, replacement?: TaskRow) => void;
}

const BLOCKED_ON: ReadonlyArray<{ value: BlockedOn; label: string }> = [
  { value: "human", label: "a person" },
  { value: "sender", label: "the sender" },
  { value: "external", label: "an external event" },
];

const ICON: Record<ReasonAction, typeof Check> = {
  accept: Check,
  complete: CheckCircle2,
  fail: XOctagon,
  wont_do: Ban,
  defer: Clock,
  block: Lock,
  unblock: LockOpen,
  cancel: XCircle,
  reassign: ArrowLeftRight,
  reopen: RotateCcw,
};

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
const DESTRUCTIVE: ReadonlySet<ReasonAction> = new Set(["fail", "wont_do", "cancel"]);
/** The owner writes a task in progress must fence with its current lease. */
const FENCED: ReadonlySet<ReasonAction> = new Set(["complete", "fail", "defer", "block"]);

export function TaskActionDialog({ open, onOpenChange, identity, action, task, inboxes = [], onDone }: Props) {
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

  const Icon = ICON[action];
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
      toast(`${ACTION_LABEL[action]}: done`, "ok");
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
      icon={<Icon size={16} />}
      description={
        <>
          {DESCRIPTION[action]} <span className="font-medium text-[var(--color-text)]">“{task.title}”</span>
        </>
      }
      footer={
        <>
          <CancelButton label="Back" />
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className={DESTRUCTIVE.has(action) ? DANGER_BTN : PRIMARY_BTN}
          >
            {busy ? "Working…" : ACTION_LABEL[action]}
          </button>
        </>
      }
    >
      {action === "accept" && (
        <Field id="task-accept-priority" label="Priority" hint="edit while accepting, optional">
          <select
            id="task-accept-priority"
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
      )}
      {action === "reassign" && (
        <Field id="task-reassign-inbox" label="To inbox">
          {targets.length === 0 ? (
            <div className="text-xs text-[var(--color-text-muted)]">No other inbox you may read or post into.</div>
          ) : (
            <select
              id="task-reassign-inbox"
              value={toInbox}
              onChange={(e) => setToInbox(e.target.value)}
              className={INPUT_CLS}
            >
              {targets.map((i) => (
                <option key={i.inbox} value={i.inbox}>
                  {i.inbox} — {i.owner_agent} ({i.owner_namespace})
                </option>
              ))}
            </select>
          )}
        </Field>
      )}
      {action === "block" && (
        <Field id="task-block-on" label="Blocked on">
          <select
            id="task-block-on"
            value={blockedOn}
            onChange={(e) => {
              const v = oneOf(
                BLOCKED_ON.map((o) => o.value),
                e.target.value,
              );
              if (v !== null) setBlockedOn(v);
            }}
            className={INPUT_CLS}
          >
            {BLOCKED_ON.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      )}
      {action === "defer" && (
        <Field id="task-defer-resume" label="Resume at" hint="the sweeper returns it to the queue then">
          <input
            id="task-defer-resume"
            type="datetime-local"
            value={resumeAt}
            onChange={(e) => setResumeAt(e.target.value)}
            className={INPUT_CLS}
          />
        </Field>
      )}
      {NEEDS_REASON.has(action) && (
        <Field id="task-action-reason" label="Reason" hint="required; shown on the card and in the feed">
          <textarea
            id="task-action-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            className={TEXTAREA_CLS}
            autoFocus
          />
        </Field>
      )}
      {(action === "accept" || action === "unblock" || action === "complete") && (
        <Field id="task-action-note" label="Note" hint="optional">
          <input
            id="task-action-note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            className={INPUT_CLS}
            autoFocus
          />
        </Field>
      )}
      {error && <ErrorNote>{error}</ErrorNote>}
    </DialogFrame>
  );
}
