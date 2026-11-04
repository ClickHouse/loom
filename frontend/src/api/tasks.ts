// The task inbox plane's client — one inbox per agent, a trust policy per poster × kind,
// a board a person edits. Wire shapes mirror `server/tasks/api/models_task.py`; the HTTP path
// is `/v1/<op>`, POST, and every body carries the caller's own `(org, namespace, agent)`.
// The dashboard writes as `loom-ui` (the picker is "view as", not the writer).
//
// Split from `api.ts` so this client rides in the Tasks view's chunk; the header bell
// imports only `taskCounts`.
import { post } from "../api";

export type TaskStatus =
  | "triage"
  | "todo"
  | "in_progress"
  | "blocked"
  | "deferred"
  | "done"
  | "wont_do"
  | "failed"
  | "cancelled";
export type Disclosure = "confidential" | "internal" | "public_ok";
export type PolicyMode = "deny" | "review" | "auto";
export type BlockedOn = "sender" | "human" | "external";

export interface TaskRow {
  task_id: string;
  org: string;
  inbox: string;
  namespace: string;
  kind: string;
  priority: number;
  disclosure: Disclosure;
  status: TaskStatus;
  reason: string;
  blocked_on: string;
  not_before: string | null;
  resume_at: string | null;
  deadline: string | null;
  defer_count: number;
  title: string;
  brief: Record<string, unknown>;
  from_agent: string;
  from_subject: string;
  origin: string;
  parent_task_id: string | null;
  owner: string;
  attempt: number;
  fence: number;
  lease_until: string | null;
  outcome: Record<string, unknown>;
  last_actor: string;
  created_at: string;
  updated_at: string;
  version: number;
}

/** The list's projection: what a row and a card render. `outcome` rides along for a done
 *  row's link (its first URL); `brief` only on `task.get`, for the opened task. */
const TASKS_LIST_FIELDS = [
  "task_id",
  "inbox",
  "kind",
  "priority",
  "status",
  "reason",
  "blocked_on",
  "resume_at",
  "deadline",
  "not_before",
  "defer_count",
  "title",
  "from_agent",
  "from_subject",
  "owner",
  "lease_until",
  "parent_task_id",
  "disclosure",
  "outcome",
  "created_at",
  "updated_at",
  "version",
];

/** A list row: the projected fields; `brief` is absent. */
export type TaskListRow = Omit<
  TaskRow,
  "brief" | "outcome" | "origin" | "attempt" | "fence" | "last_actor" | "org" | "namespace"
> &
  Partial<Pick<TaskRow, "brief" | "outcome" | "origin" | "attempt" | "fence" | "last_actor" | "org" | "namespace">>;

export interface TaskEvent {
  seq: number;
  task_id: string;
  inbox: string;
  event: string;
  from_status: string;
  to_status: string;
  actor: string;
  agent: string;
  fence: number;
  version: number;
  reason: string;
  patch: Record<string, unknown>;
  note: string;
  ts: string;
}

export interface Inbox {
  org: string;
  inbox: string;
  owner_agent: string;
  owner_namespace: string;
  description: string;
  posters: string[];
  accept_disclosure: Disclosure[];
  default_lease_seconds: number;
  max_attempts: number;
  max_defers: number;
  triage_escalate_after_seconds: number;
  webhook_url: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface PosterPolicy {
  org: string;
  inbox: string;
  poster: string;
  kind: string;
  mode: PolicyMode;
  /** Brief fields the poster must supply — a shape check at create, not an
   *  acceptance decision. */
  require_evidence: string[];
  set_by: string;
  set_at: string;
  version: number;
}

export interface InboxCount {
  inbox: string;
  status: TaskStatus | string;
  /** On blocked rows: sender | human | external; "" otherwise. */
  blocked_on: string;
  n: number;
  oldest: string | null;
}

export interface Caller {
  org: string;
  namespace: string;
}

const UI_AGENT = "loom-ui";

function body(id: Caller, extra: object): Record<string, unknown> {
  return { org: id.org, namespace: id.namespace, agent: UI_AGENT, ...extra };
}

/** A lease-fenced write from a person. The fence proves the lease is current; agent `-`
 *  lets the owner side act on a task another agent holds (the actor is the token's). */
function fenced(id: Caller, extra: object): Record<string, unknown> {
  return { ...body(id, extra), agent: "-" };
}

/** A person's claim runs a day: the inbox's default lease is sized for an agent that
 *  heartbeats, and a person who does not would see the task swept back to the queue. */
const PERSON_LEASE_SECONDS = 86_400;

// ---------------------------------------------------------------- inbox.*

/** Inboxes owned by `namespace`; with `mineEverywhere`, every inbox in the
 *  org the caller may post to (write on its owner namespace, or listed as a poster) —
 *  the reassign picker. */
export async function listInboxes(id: Caller, opts: { mineEverywhere?: boolean } = {}): Promise<Inbox[]> {
  const out = await post<{ inboxes: Inbox[] }>(
    "/v1/inbox.list",
    body(id, {
      mine_everywhere: opts.mineEverywhere ?? false,
      limit: 200,
    }),
  );
  return out.inboxes || [];
}

export interface InboxRegisterInput {
  inbox: string;
  owner_agent: string;
  description?: string;
  posters?: string[];
  accept_disclosure?: Disclosure[];
  default_lease_seconds?: number;
  max_attempts?: number;
  max_defers?: number;
  triage_escalate_after_seconds?: number;
  webhook_url?: string;
  if_match_version?: number | null;
}

/** Admin side: `namespace` is the owner namespace. Re-registering an existing
 *  inbox is its settings edit; `if_match_version` guards a racing one. */
export async function registerInbox(
  id: Caller,
  input: InboxRegisterInput,
): Promise<{ inbox: Inbox; created: boolean }> {
  return post("/v1/inbox.register", body(id, input));
}

/** One identity loom already knows in the org — the poster picker's row.
 *  People come under their login subject (an email) with kind `human`;
 *  bots under `sa:` subjects with the service-account name as `label`. */
export interface Principal {
  subject: string;
  label: string;
  kind: "human" | "service_account" | "other";
  namespaces: string[];
  roles: string[];
  last_seen: string | null;
  /** Already a writer on the inbox's owner namespace: posts without being listed. */
  can_already_post: boolean;
  /** In the inbox's posters list right now. */
  listed: boolean;
}

/** Admin side: the identities the org knows, marked against `inbox` — who
 *  can already post there and who is listed. The posters list is chosen from
 *  these, never typed. A listed subject the directory does not return is
 *  still included by the server so it can be unchecked. */
export async function listPrincipals(
  id: Caller,
  opts: { inbox: string; query?: string; limit?: number },
): Promise<{ principals: Principal[]; truncated: boolean }> {
  const out = await post<{ principals: Principal[]; truncated: boolean }>(
    "/v1/inbox.principals",
    body(id, {
      inbox: opts.inbox,
      query: opts.query ?? "",
      limit: opts.limit ?? 200,
    }),
  );
  return { principals: out.principals || [], truncated: !!out.truncated };
}

export async function listPolicies(id: Caller, inbox: string): Promise<PosterPolicy[]> {
  const out = await post<{ policies: PosterPolicy[] }>("/v1/inbox.policy_list", body(id, { inbox }));
  return out.policies || [];
}

export interface PolicySetInput {
  inbox: string;
  poster: string;
  kind: string;
  mode: PolicyMode;
  require_evidence?: string[];
  remove?: boolean;
}

export async function setPolicy(
  id: Caller,
  input: PolicySetInput,
): Promise<{ policy: PosterPolicy | null; removed: boolean }> {
  return post("/v1/inbox.policy_set", body(id, input));
}

// ---------------------------------------------------------------- reads

export async function listTasks(
  id: Caller,
  opts: {
    inbox?: string;
    postedByMe?: boolean;
    postedBy?: string;
    statuses?: TaskStatus[];
    kinds?: string[];
    owner?: string;
    limit?: number;
    fields?: string[];
  } = {},
): Promise<{ tasks: TaskListRow[]; truncated: boolean }> {
  const out = await post<{ tasks: TaskListRow[]; truncated: boolean }>(
    "/v1/task.list",
    body(id, {
      ...(opts.inbox ? { inbox: opts.inbox } : {}),
      posted_by_me: opts.postedByMe ?? false,
      // Owner side: one poster's tasks in the inbox — the track record beside
      // a triage decision (subject or agent).
      ...(opts.postedBy ? { posted_by: opts.postedBy } : {}),
      statuses: opts.statuses ?? [],
      kinds: opts.kinds ?? [],
      ...(opts.owner ? { owner: opts.owner } : {}),
      limit: opts.limit ?? 200,
      fields: opts.fields ?? TASKS_LIST_FIELDS,
    }),
  );
  return { tasks: out.tasks || [], truncated: !!out.truncated };
}

export async function getTask(id: Caller, taskId: string): Promise<TaskRow | null> {
  const out = await post<{ found: boolean; task: TaskRow | null }>("/v1/task.get", body(id, { task_id: taskId }));
  return out.found ? out.task : null;
}

/** Per inbox × status counts for the badge — numbers, never rows. Without
 *  `inbox`, every inbox owned by `namespace`. */
export async function taskCounts(id: Caller, inbox?: string): Promise<InboxCount[]> {
  const out = await post<{ counts: InboxCount[] }>("/v1/task.counts", body(id, inbox ? { inbox } : {}));
  return out.counts || [];
}

export async function taskHistory(id: Caller, taskId: string, limit = 200): Promise<TaskEvent[]> {
  const out = await post<{ found: boolean; events: TaskEvent[] }>(
    "/v1/task.history",
    body(id, { task_id: taskId, limit }),
  );
  return out.events || [];
}

// ---------------------------------------------------------------- writes

export interface TaskCreateInput {
  inbox: string;
  kind: string;
  title: string;
  brief?: Record<string, unknown>;
  priority?: number;
  disclosure?: Disclosure;
  not_before?: string | null;
  deadline?: string | null;
  origin?: string;
  parent_task_id?: string | null;
  idempotency_key?: string;
}

export async function createTask(
  id: Caller,
  input: TaskCreateInput,
): Promise<{ task: TaskRow; created: boolean; seq: number }> {
  return post("/v1/task.create", body(id, input));
}

export interface TaskUpdateInput {
  task_id: string;
  if_match_version: number;
  title?: string;
  brief?: Record<string, unknown>;
  priority?: number;
  deadline?: string | null;
  not_before?: string | null;
  clear_deadline?: boolean;
  clear_not_before?: boolean;
  note?: string;
}

/** Edit the brief under optimistic concurrency. A stale `if_match_version`
 *  is a 409 PRECONDITION_FAILED whose `detail` carries `current_version` and
 *  `current` (the row as it is now) — see `conflictOf`. */
export async function updateTask(id: Caller, input: TaskUpdateInput): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.update", body(id, input));
}

export async function acceptTask(
  id: Caller,
  taskId: string,
  opts: { priority?: number; constraints?: string; note?: string } = {},
): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.accept", body(id, { task_id: taskId, ...opts }));
}

export async function deferTask(
  id: Caller,
  taskId: string,
  reason: string,
  resumeAt: string | null,
  fence = 0,
): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.defer", (fence ? fenced : body)(id, { task_id: taskId, fence, reason, resume_at: resumeAt }));
}

export async function blockTask(
  id: Caller,
  taskId: string,
  reason: string,
  blockedOn: BlockedOn,
  fence = 0,
): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.block", (fence ? fenced : body)(id, { task_id: taskId, fence, reason, blocked_on: blockedOn }));
}

/** To do → in progress, held by the dashboard (owner `loom-ui`); 409 TASK_NOT_YET while
 *  `not_before` is ahead, TASK_ALREADY_CLAIMED when an agent won the lease first. */
export async function claimTask(id: Caller, taskId: string): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.claim", body(id, { task_id: taskId, lease_seconds: PERSON_LEASE_SECONDS }));
}

export async function completeTask(
  id: Caller,
  taskId: string,
  fence: number,
  note = "",
): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.complete", fenced(id, { task_id: taskId, fence, note }));
}

export async function failTask(
  id: Caller,
  taskId: string,
  fence: number,
  reason: string,
): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.fail", fenced(id, { task_id: taskId, fence, reason }));
}

export async function unblockTask(id: Caller, taskId: string, note = ""): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.unblock", body(id, { task_id: taskId, note }));
}

/** A deferred task back to the queue now, ahead of its `resume_at`
 *  (409 TASK_NOT_DEFERRED for any other status). */
export async function resumeTask(id: Caller, taskId: string, note = ""): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.resume", body(id, { task_id: taskId, note }));
}

export async function wontDoTask(id: Caller, taskId: string, reason: string): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.wont_do", body(id, { task_id: taskId, reason }));
}

export async function cancelTask(id: Caller, taskId: string, reason: string): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.cancel", body(id, { task_id: taskId, reason }));
}

export async function reopenTask(id: Caller, taskId: string, reason: string): Promise<{ task: TaskRow; seq: number }> {
  return post("/v1/task.reopen", body(id, { task_id: taskId, reason }));
}

export async function reassignTask(
  id: Caller,
  taskId: string,
  toInbox: string,
  reason: string,
): Promise<{ task: TaskRow; replacement: TaskRow }> {
  return post("/v1/task.reassign", body(id, { task_id: taskId, to_inbox: toInbox, reason }));
}
