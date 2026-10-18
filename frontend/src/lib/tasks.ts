// The Tasks page's vocabulary — the five groups over the nine statuses ("what happens
// next", not the status enum), the attention tiles, the queue order, the one line a row
// says for its state, which actions a status offers, the posters' avatars, and the shape
// of a task.update conflict. Pure functions, so the view, the board, the panel and the
// tests share one reading and the tests need no DOM.
import { ApiError } from "./errors";
import type { PosterPolicy, TaskEvent, TaskListRow, TaskRow, TaskStatus } from "../api/tasks";
import { displaySubject, listTime, parseChTime } from "./format";
import { isRecord } from "./guards";

// ------------------------------------------------------------------ time

/** A ClickHouse (naive UTC) or ISO time → ms, read as every list reads it (`parseChTime`);
 *  NaN when unreadable. */
function parseTs(iso: string | null | undefined): number {
  return parseChTime(iso)?.getTime() ?? NaN;
}

/** "42m ago" / "in 2h" / "3d ago": the date rule every list follows (`listTime` in
 *  lib/format.ts), measured from the page's ticking `now`, with the countdowns listTime
 *  has no form for ("in 2h", past a week "on Sep 29"). */
export function rel(iso: string | null | undefined, now = Date.now()): string {
  const t = parseTs(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.floor((now - t) / 1000);
  if (s > -60 && s < 60) return "just now";
  const a = Math.abs(s);
  const span =
    a < 3600 ? `${Math.floor(a / 60)}m` : a < 86400 ? `${Math.floor(a / 3600)}h` : `${Math.floor(a / 86400)}d`;
  if (a < 7 * 86400) return s > 0 ? `${span} ago` : `in ${span}`;
  return s > 0 ? listTime(iso) : `on ${listTime(iso)}`;
}

/** Whole minutes left on a lease, never negative; null without a lease. */
export function leaseMinutesLeft(leaseUntil: string | null | undefined, now = Date.now()): number | null {
  const t = parseTs(leaseUntil);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((t - now) / 60_000));
}

const LEASE_SOON_MINUTES = 10;

/** In progress with ten minutes or less on the lease — the fourth tile. */
export function leaseEndingSoon(r: Pick<TaskListRow, "status" | "lease_until">, now = Date.now()): boolean {
  if (r.status !== "in_progress") return false;
  const left = leaseMinutesLeft(r.lease_until, now);
  return left !== null && left <= LEASE_SOON_MINUTES;
}

// ---------------------------------------------------------------- groups

/** Five groups by what happens next: a person decides (Triage), an agent
 *  pulls (To do), a lease runs (In progress), something else must happen
 *  first (Waiting: blocked or deferred), or it is over (Closed). */
export type Group = "triage" | "todo" | "in_progress" | "waiting" | "closed";

export const GROUPS: readonly Group[] = ["triage", "todo", "in_progress", "waiting", "closed"];

export const GROUP_META: Record<Group, { label: string; color: string; hint: string }> = {
  triage: { label: "Triage", color: "var(--color-accent)", hint: "decide: accept or won't do" },
  todo: { label: "To do", color: "var(--color-text-muted)", hint: "waiting for an agent to pull" },
  in_progress: { label: "In progress", color: "var(--color-info)", hint: "under a lease" },
  waiting: { label: "Waiting", color: "var(--color-warn)", hint: "blocked or deferred, with a reason" },
  closed: { label: "Closed", color: "var(--color-surface-3)", hint: "done · won't do · failed · cancelled" },
};

const CLOSED_STATUSES: readonly TaskStatus[] = ["done", "wont_do", "failed", "cancelled"];

export function groupOf(status: TaskStatus | string): Group {
  if (status === "blocked" || status === "deferred") return "waiting";
  if ((CLOSED_STATUSES as readonly string[]).includes(status)) return "closed";
  if (status === "triage" || status === "in_progress") return status;
  return "todo";
}

/** Priority first (1 is the most urgent), then age (the oldest first) — the
 *  order `task.pull` claims in, so each group reads as the queue. */
export function sortTasks<T extends Pick<TaskListRow, "priority" | "created_at" | "task_id">>(rows: readonly T[]): T[] {
  return [...rows].sort(
    (a, b) =>
      a.priority - b.priority ||
      (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0) ||
      (a.task_id < b.task_id ? -1 : a.task_id > b.task_id ? 1 : 0),
  );
}

/** Rows per group, each group in queue order. */
export function groupTasks<T extends Pick<TaskListRow, "status" | "priority" | "created_at" | "task_id">>(
  rows: readonly T[],
): Record<Group, T[]> {
  const out: Record<Group, T[]> = { triage: [], todo: [], in_progress: [], waiting: [], closed: [] };
  for (const r of sortTasks(rows)) out[groupOf(r.status)].push(r);
  return out;
}

export function kindsOf(rows: readonly Pick<TaskListRow, "kind">[]): string[] {
  return [...new Set(rows.map((r) => r.kind))].sort();
}

// ------------------------------------------------------------- attention

/** The four states that need a person: a decision, a block on a human, a
 *  failure to reopen or close, a lease about to run out. */
export type Attention = "triage" | "human" | "failed" | "soon";

export const ATTENTIONS: readonly Attention[] = ["triage", "human", "failed", "soon"];

/** Each tile's label, and its item in a phone's Filters menu. */
export const ATTENTION_META: Record<Attention, { label: string }> = {
  triage: { label: "Triage · decide" },
  human: { label: "Blocked on a person" },
  failed: { label: "Failed · reopen or close" },
  soon: { label: `Lease ending ≤${LEASE_SOON_MINUTES} min` },
};

export function matchesAttention(
  r: Pick<TaskListRow, "status" | "blocked_on" | "lease_until">,
  key: Attention,
  now = Date.now(),
): boolean {
  switch (key) {
    case "triage":
      return r.status === "triage";
    case "human":
      return r.status === "blocked" && r.blocked_on === "human";
    case "failed":
      return r.status === "failed";
    case "soon":
      return leaseEndingSoon(r, now);
  }
}

export function attentionCounts(
  rows: readonly Pick<TaskListRow, "status" | "blocked_on" | "lease_until">[],
  now = Date.now(),
): Record<Attention, number> {
  const out: Record<Attention, number> = { triage: 0, human: 0, failed: 0, soon: 0 };
  for (const r of rows) for (const k of ATTENTIONS) if (matchesAttention(r, k, now)) out[k] += 1;
  return out;
}

// --------------------------------------------------------------- filters

export type PosterFilter = "me" | "others";

export interface Filters {
  attention: Attention | null;
  kind: string | null;
  poster: PosterFilter | null;
  /** The toolbar's search: every word must appear in the title, kind, poster, reason or id. */
  q: string;
}

export const NO_FILTERS: Filters = { attention: null, kind: null, poster: null, q: "" };

const POSTER_FILTER_LABEL: Record<PosterFilter, string> = { me: "this agent", others: "others" };

/** What is in force, as the filtered state names it: `"recall" · kind:docs · posted by others`. */
export function filterSummary(f: Filters): string {
  const parts: string[] = [];
  if (f.q.trim()) parts.push(`"${f.q.trim()}"`);
  if (f.kind) parts.push(`kind:${f.kind}`);
  if (f.poster) parts.push(`posted by ${POSTER_FILTER_LABEL[f.poster]}`);
  if (f.attention) parts.push(ATTENTION_META[f.attention].label);
  return parts.join(" · ");
}

/** Own work and handed work: a row whose `from_agent` is the inbox's own
 *  agent (and that no person posted) is the agent's own task. */
export type PosterKind = "me" | "bot" | "human";

export function posterKind(
  r: Pick<TaskListRow, "from_agent" | "from_subject">,
  ownerAgent: string | undefined,
): PosterKind {
  if (r.from_subject && !r.from_subject.startsWith("sa:")) return "human";
  if (ownerAgent && r.from_agent === ownerAgent) return "me";
  return "bot";
}

/** Does a row pass the page's filters? Closed rows pass: their lane is folded until opened. */
export function passesFilters(
  r: Pick<
    TaskListRow,
    | "status"
    | "blocked_on"
    | "lease_until"
    | "kind"
    | "from_agent"
    | "from_subject"
    | "inbox"
    | "title"
    | "reason"
    | "task_id"
  >,
  f: Filters,
  ownerAgentOf: (inbox: string) => string | undefined,
  now = Date.now(),
): boolean {
  if (f.attention && !matchesAttention(r, f.attention, now)) return false;
  if (f.kind && r.kind !== f.kind) return false;
  if (f.poster) {
    const mine = posterKind(r, ownerAgentOf(r.inbox)) === "me";
    if (f.poster === "me" && !mine) return false;
    if (f.poster === "others" && mine) return false;
  }
  const words = f.q.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length > 0) {
    const text = [r.title, r.kind, posterName(r), r.reason, r.task_id].join(" ").toLowerCase();
    if (!words.every((w) => text.includes(w))) return false;
  }
  return true;
}

/** Who has the task in hand: its owner once claimed, else the agent the inbox is for. */
export function assigneeOf(
  r: Pick<TaskListRow, "owner">,
  ownerAgent: string | undefined,
): { name: string; label: string } | null {
  if (r.owner) return { name: r.owner, label: `Claimed by ${r.owner}` };
  if (ownerAgent) return { name: ownerAgent, label: `For ${ownerAgent}, the inbox's agent` };
  return null;
}

// --------------------------------------------------------------- posters

/** The poster as a person reads it: a login subject, a bot's name, the agent. */
export function posterName(r: Pick<TaskListRow, "from_agent" | "from_subject">): string {
  if (r.from_subject)
    return r.from_subject.startsWith("sa:") ? r.from_agent || displaySubject(r.from_subject) : r.from_subject;
  return r.from_agent || "—";
}

/** Two letters for an avatar: the initials of the first two words
 *  (triage-bot → "tb", jane.doe@… → "jd"), else the first two. */
export function initials(name: string): string {
  const base = name.replace(/^(sa|human|process):/, "").split("@")[0];
  const words = base.split(/[-_.\s]+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toLowerCase();
  return base.slice(0, 2).toLowerCase() || "?";
}

export function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

// ---------------------------------------------------------------- rows

/** The one line under a title: what matters for the row's state. */
export interface Subline {
  text: string;
  /** A reason or a progress note reads in italics. */
  italic?: boolean;
  /** A second, plain part ("· resumes in 2 h"). */
  suffix?: string;
  /** For a done row, the outcome's first URL. */
  href?: string;
}

const URL_RE = /^https?:\/\/\S+$/i;

/** The first URL in an outcome's values (depth one), or null. */
export function firstUrl(obj: Record<string, unknown> | null | undefined): string | null {
  for (const v of Object.values(obj ?? {})) {
    if (typeof v === "string" && URL_RE.test(v)) return v;
    if (Array.isArray(v)) {
      const s = v.find((x): x is string => typeof x === "string" && URL_RE.test(x));
      if (s) return s;
    }
  }
  return null;
}

export function sublineOf(
  r: Pick<
    TaskListRow,
    | "status"
    | "reason"
    | "blocked_on"
    | "resume_at"
    | "not_before"
    | "from_agent"
    | "from_subject"
    | "created_at"
    | "outcome"
  >,
  now = Date.now(),
  progressNote?: string,
): Subline | null {
  switch (r.status) {
    case "blocked":
      return { text: `blocked on ${r.blocked_on || "a person"}${r.reason ? `: ${r.reason}` : ""}`, italic: true };
    case "deferred":
      return {
        text: r.reason || "deferred",
        italic: true,
        suffix: r.resume_at ? `· resumes ${rel(r.resume_at, now)}` : undefined,
      };
    case "wont_do":
    case "failed":
    case "cancelled":
      return r.reason ? { text: r.reason, italic: true } : null;
    case "done": {
      const u = firstUrl(r.outcome);
      return u ? { text: "done ·", href: u } : null;
    }
    case "todo": {
      const t = parseTs(r.not_before);
      const from = `from ${posterName(r)}`;
      return !Number.isNaN(t) && t > now
        ? { text: from, suffix: `· scheduled ${rel(r.not_before, now)}` }
        : { text: from };
    }
    case "in_progress":
      return progressNote ? { text: progressNote, italic: true } : null;
    case "triage":
      return { text: `from ${posterName(r)}` };
  }
}

/** The newest progress note in a task's history, "" without one. */
export function latestProgressNote(events: readonly TaskEvent[]): string {
  let best: TaskEvent | null = null;
  for (const e of events) if (e.event === "progress" && e.note && (!best || e.seq > best.seq)) best = e;
  return best?.note ?? "";
}

// -------------------------------------------------------------- statuses

/** The pill: a label a reader can tell apart without its colour, and its dot. */
export const STATUS_META: Record<TaskStatus, { label: string; dot: string }> = {
  triage: { label: "triage", dot: "var(--color-accent)" },
  todo: { label: "to do", dot: "var(--color-text-muted)" },
  in_progress: { label: "in progress", dot: "var(--color-info)" },
  blocked: { label: "blocked", dot: "var(--color-warn)" },
  deferred: { label: "deferred", dot: "var(--color-cat-5)" },
  done: { label: "done", dot: "var(--color-success)" },
  wont_do: { label: "won't do", dot: "var(--color-text-muted)" },
  failed: { label: "failed", dot: "var(--color-danger)" },
  cancelled: { label: "cancelled", dot: "var(--color-text-muted)" },
};

export const PRIORITY_LABEL: Record<number, string> = { 1: "P1 urgent", 2: "P2 high", 3: "P3 normal", 4: "P4 low" };

// --------------------------------------------------------------- actions

/** The transitions a status offers a person (the state machine in
 *  `server/tasks/app/transitions.py`). `edit` is `task.update`, allowed while the
 *  task is editable; `resume` is `task.resume`, a deferred task back to the
 *  queue now; `start` is `task.claim` (to do → in progress under the page's own
 *  lease); `complete` and `fail` are the owner's finish, fenced by the lease. */
export type TaskAction =
  | "accept"
  | "start"
  | "complete"
  | "fail"
  | "wont_do"
  | "defer"
  | "block"
  | "unblock"
  | "resume"
  | "cancel"
  | "reassign"
  | "edit"
  | "reopen";

export function actionsFor(status: TaskStatus): TaskAction[] {
  switch (status) {
    case "triage":
      return ["accept", "wont_do", "edit", "reassign", "cancel"];
    case "todo":
      return ["start", "defer", "block", "wont_do", "cancel", "reassign", "edit"];
    case "blocked":
      return ["unblock", "wont_do", "cancel", "edit"];
    case "deferred":
      return ["resume", "wont_do", "cancel", "edit"];
    case "in_progress":
      return ["complete", "fail", "block", "defer", "wont_do", "cancel"];
    case "failed":
    case "wont_do":
    case "cancelled":
      return ["reopen"];
    case "done":
      return [];
  }
}

export const ACTION_LABEL: Record<TaskAction, string> = {
  accept: "Accept",
  start: "Start",
  complete: "Mark done",
  fail: "Fail",
  wont_do: "Won't do",
  defer: "Defer",
  block: "Block",
  unblock: "Unblock",
  resume: "Resume now",
  cancel: "Cancel task",
  reassign: "Reassign",
  edit: "Edit brief",
  reopen: "Reopen",
};

/** A move's name in a menu: the ones that open the action dialog end in "…". */
export function menuLabel(a: TaskAction): string {
  return DIALOG_ACTIONS.has(a) ? `${ACTION_LABEL[a]}…` : ACTION_LABEL[a];
}

/** What the toast says once an action went through: what happened, to which task. */
export function actionDone(action: TaskAction, title: string): string {
  const t = `“${title}”`;
  switch (action) {
    case "accept":
      return `Accepted ${t}: it moved to To do`;
    case "start":
      return `Started ${t}`;
    case "complete":
      return `Marked ${t} done`;
    case "fail":
      return `Marked ${t} failed`;
    case "wont_do":
      return `Closed ${t} as won't do`;
    case "defer":
      return `Deferred ${t}`;
    case "block":
      return `Blocked ${t}`;
    case "unblock":
      return `Unblocked ${t}: it is back in To do`;
    case "resume":
      return `Resumed ${t}: it is back in To do`;
    case "cancel":
      return `Cancelled ${t}`;
    case "reassign":
      return `Reassigned ${t}`;
    case "edit":
      return `Saved ${t}`;
    case "reopen":
      return `Reopened ${t}`;
  }
}

/** Actions that take a reason or a choice and so open the action dialog;
 *  the rest (accept, start, unblock, resume) fire on the click. */
export const DIALOG_ACTIONS: ReadonlySet<TaskAction> = new Set([
  "complete",
  "fail",
  "wont_do",
  "defer",
  "block",
  "cancel",
  "reassign",
  "reopen",
]);

/** What dropping a task on a group does: the first of the group's entry moves
 *  the task's status offers. Null when the state machine has no one-step move
 *  there (into Triage, or back from In progress to To do, or its own group). */
const DROP_ACTIONS: Record<Group, readonly TaskAction[]> = {
  triage: [],
  todo: ["accept", "unblock", "resume", "reopen"],
  in_progress: ["start"],
  waiting: ["block"],
  closed: ["complete", "wont_do"],
};

export function dropAction(status: TaskStatus, to: Group): TaskAction | null {
  const offered = actionsFor(status);
  return DROP_ACTIONS[to].find((a) => offered.includes(a)) ?? null;
}

// -------------------------------------------------------------- conflict

/** The fields of the current row the conflict view shows; each is present
 *  only when the server sent it with the right type. */
export type ConflictRow = Partial<Pick<TaskRow, "title" | "brief" | "status" | "last_actor">>;

export interface UpdateConflict {
  currentVersion: number;
  current: ConflictRow | null;
}

function isTaskStatus(v: unknown): v is TaskStatus {
  return typeof v === "string" && Object.hasOwn(STATUS_META, v);
}

function conflictRow(d: Record<string, unknown>): ConflictRow {
  const row: ConflictRow = {};
  if (typeof d.title === "string") row.title = d.title;
  if (isRecord(d.brief)) row.brief = d.brief;
  if (isTaskStatus(d.status)) row.status = d.status;
  if (typeof d.last_actor === "string") row.last_actor = d.last_actor;
  return row;
}

/** A `task.update` refused because the row moved: 409 PRECONDITION_FAILED
 *  with the current version and row in `detail`. Null for any other error. */
export function conflictOf(err: unknown): UpdateConflict | null {
  if (!(err instanceof ApiError) || err.status !== 409 || err.code !== "PRECONDITION_FAILED") return null;
  const v = err.detail.current_version;
  const current = err.detail.current;
  return {
    currentVersion: typeof v === "number" ? v : Number(v ?? 0),
    current: isRecord(current) ? conflictRow(current) : null,
  };
}

// -------------------------------------------------------------- timeline

/** Heartbeats are the lease's pulse, not the task's story: a run of them
 *  collapses to one line saying how many. Works in either time order. */
export function collapseHeartbeats(events: readonly TaskEvent[]): Array<TaskEvent & { collapsed?: number }> {
  const out: Array<TaskEvent & { collapsed?: number }> = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (e.event === "heartbeat" && last && last.event === "heartbeat") {
      last.collapsed = (last.collapsed ?? 1) + 1;
      last.ts = e.ts;
      continue;
    }
    out.push({ ...e });
  }
  return out;
}

type ActorKind = "human" | "sweeper" | "bot";

/** Who acted: a person (`human:` or a login subject), the lease sweeper
 *  (`process:task_lease_sweep`), or an agent. */
export function actorKind(actor: string, agent = ""): ActorKind {
  const a = actor || agent;
  if (a.startsWith("human:") || (a.includes("@") && !a.startsWith("process:") && !a.startsWith("sa:"))) return "human";
  if (a.startsWith("process:task_") || a.startsWith("task_")) return "sweeper";
  return "bot";
}

export function actorName(actor: string, agent = ""): string {
  const a = actor || agent;
  return displaySubject(a.replace(/^(human|process):/, ""));
}

export const EVENT_LABEL: Record<string, string> = {
  created: "posted",
  accepted: "accepted",
  claimed: "claimed",
  heartbeat: "heartbeat",
  progress: "progress",
  deferred: "deferred",
  blocked: "blocked",
  unblocked: "unblocked",
  done: "completed",
  failed: "failed",
  wont_do: "won't do",
  cancelled: "cancelled",
  reopened: "reopened",
  updated: "edited",
  reassigned: "reassigned",
  lease_expired: "lease expired",
  escalated: "escalated",
  triage_escalated: "escalated",
  resumed: "resumed",
  "code.coverage.ready": "coverage ready",
};

// ------------------------------------------------------------ the brief

interface BriefSections {
  objective: string;
  criteria: string[];
  /** Everything else the poster put in the brief, `inputs` flattened in. */
  inputs: Array<[string, unknown]>;
  constraints: string;
}

const BRIEF_OWN_KEYS = new Set(["objective", "acceptance_criteria", "constraints", "inputs"]);

export function briefSections(brief: Record<string, unknown> | null | undefined): BriefSections {
  const b = brief ?? {};
  const objective =
    typeof b.objective === "string" ? b.objective : b.objective == null ? "" : JSON.stringify(b.objective);
  const c = b.acceptance_criteria;
  const criteria = Array.isArray(c) ? c.map(String) : typeof c === "string" && c ? [c] : [];
  const constraints =
    typeof b.constraints === "string"
      ? b.constraints
      : Array.isArray(b.constraints)
        ? b.constraints.map(String).join("\n")
        : "";
  const inputs: Array<[string, unknown]> = [];
  if (isRecord(b.inputs)) for (const [k, v] of Object.entries(b.inputs)) inputs.push([k, v]);
  for (const k of Object.keys(b).sort()) if (!BRIEF_OWN_KEYS.has(k)) inputs.push([k, b[k]]);
  return { objective, criteria, inputs, constraints };
}

type InputKind = "url" | "issue" | "span" | "file" | "hex" | "text";

/** How an input value renders: a URL and an `owner/repo#123` link, a
 *  `file:line` span, a `.sql` / `.sh` name and a short SHA in mono. */
export function inputKind(v: unknown): InputKind {
  if (typeof v !== "string") return "text";
  if (URL_RE.test(v)) return "url";
  if (/^(?:[\w.-]+\/[\w.-]+)?#\d+$/.test(v)) return "issue";
  if (/^[\w./-]+:\d+(?:-\d+)?$/.test(v)) return "span";
  if (/\.(sql|sh|py|cpp|h|ts|tsx|js|reference|json|yaml|yml|txt)$/i.test(v) || /^\d{5}_[\w-]+$/.test(v)) return "file";
  if (/^[0-9a-f]{7,40}$/.test(v)) return "hex";
  return "text";
}

/** `owner/repo#123` → its GitHub page; a bare `#123` names no repository. */
export function issueHref(v: string): string | null {
  const m = /^([\w.-]+\/[\w.-]+)#(\d+)$/.exec(v);
  return m ? `https://github.com/${m[1]}/issues/${m[2]}` : null;
}

// ---------------------------------------------------------- the poster

/** What one poster sent to this inbox before, by status — from one
 *  `task.list posted_by` read with the fields it needs. */
export interface PosterRecord {
  done: number;
  wont_do: number;
  failed: number;
  open: number;
  total: number;
  truncated: boolean;
}

export function tallyRecord(rows: readonly Pick<TaskListRow, "status">[], truncated: boolean): PosterRecord {
  const r: PosterRecord = { done: 0, wont_do: 0, failed: 0, open: 0, total: rows.length, truncated };
  for (const t of rows) {
    if (t.status === "done" || t.status === "wont_do" || t.status === "failed") r[t.status] += 1;
    else if (t.status !== "cancelled") r.open += 1;
  }
  return r;
}

/** The trust rule that applies: the poster's subject, then its agent, then
 *  `*`; an exact kind before the `*` kind. */
export function resolvePolicy(
  policies: readonly PosterPolicy[],
  task: Pick<TaskRow, "from_subject" | "from_agent" | "kind">,
): PosterPolicy | null {
  const posters = [task.from_subject, task.from_agent, "*"].filter(Boolean);
  for (const p of posters) {
    const exact = policies.find((x) => x.poster === p && x.kind === task.kind);
    if (exact) return exact;
    const any = policies.find((x) => x.poster === p && x.kind === "*");
    if (any) return any;
  }
  return null;
}

/** Which of the rule's required evidence fields the brief carries (a
 *  dotted path reaches into `inputs`). */
export function evidenceStatus(
  required: readonly string[],
  brief: Record<string, unknown> | null | undefined,
): { present: string[]; missing: string[] } {
  const present: string[] = [];
  const missing: string[] = [];
  for (const path of required) {
    let cur: unknown = brief ?? {};
    // Only objects are walked: an array along the path reads as absent (no brief nests one).
    for (const part of path.split(".")) cur = isRecord(cur) ? cur[part] : undefined;
    if (cur === undefined && isRecord(brief?.inputs)) cur = brief.inputs[path];
    (cur === undefined || cur === null || cur === "" ? missing : present).push(path);
  }
  return { present, missing };
}

// ------------------------------------------------------------ the form

/** A datetime-local input's value → the ISO instant the server stores; "" → null. */
export function isoFromLocal(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** The server's naive-UTC or ISO instant → a datetime-local input's value. */
export function localFromIso(iso: string | null | undefined): string {
  if (!iso) return "";
  const t = parseTs(iso);
  if (Number.isNaN(t)) return "";
  const d = new Date(t);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
