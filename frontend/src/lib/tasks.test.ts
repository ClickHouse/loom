import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./errors";
import {
  actionDone,
  actionsFor,
  assigneeOf,
  actorKind,
  attentionCounts,
  briefSections,
  collapseHeartbeats,
  conflictOf,
  dropAction,
  evidenceStatus,
  filterSummary,
  firstUrl,
  groupOf,
  groupTasks,
  initials,
  inputKind,
  isoFromLocal,
  issueHref,
  kindsOf,
  latestProgressNote,
  leaseEndingSoon,
  leaseMinutesLeft,
  localFromIso,
  matchesAttention,
  passesFilters,
  posterKind,
  posterName,
  rel,
  resolvePolicy,
  sortTasks,
  sublineOf,
  tallyRecord,
  type Filters,
} from "./tasks";
import { attentionByInbox, bellCount } from "./taskBell";
import { listTime } from "./format";
import type { InboxCount, PosterPolicy, TaskEvent, TaskListRow, TaskStatus } from "../api/tasks";
import { must } from "../test/must";

// The Tasks page's vocabulary — five groups by what happens next, four attention tiles,
// the one line a row says per state.

const NOW = Date.parse("2026-09-20T12:00:00Z");
const ago = (min: number) =>
  new Date(NOW - min * 60_000)
    .toISOString()
    .replace("Z", "")
    .replace(/\.\d+$/, "");

const row = (id: string, extra: Partial<TaskListRow> = {}): TaskListRow => ({
  task_id: id,
  inbox: "private-bot",
  kind: "bug",
  priority: 3,
  status: "todo",
  reason: "",
  blocked_on: "",
  not_before: null,
  resume_at: null,
  deadline: null,
  defer_count: 0,
  title: id,
  from_agent: "reporter-public",
  from_subject: "sa:reporter-public-1a2b3c",
  owner: "",
  lease_until: null,
  parent_task_id: null,
  disclosure: "internal",
  created_at: ago(60),
  updated_at: ago(60),
  version: 1,
  ...extra,
});

describe("groups: what happens next, not the status enum", () => {
  it("folds nine statuses into five groups", () => {
    const want: Record<TaskStatus, string> = {
      triage: "triage",
      todo: "todo",
      in_progress: "in_progress",
      blocked: "waiting",
      deferred: "waiting",
      done: "closed",
      wont_do: "closed",
      failed: "closed",
      cancelled: "closed",
    };
    for (const [s, g] of Object.entries(want)) expect(groupOf(s), s).toBe(g);
  });

  it("groups rows and keeps queue order inside a group — priority, then the oldest", () => {
    const g = groupTasks([
      row("late-p1", { priority: 1, created_at: ago(10) }),
      row("early-p1", { priority: 1, created_at: ago(120) }),
      row("p3", { priority: 3 }),
      row("blocked", { status: "blocked" }),
      row("deferred", { status: "deferred" }),
      row("done", { status: "done" }),
      row("failed", { status: "failed" }),
      row("triage", { status: "triage" }),
    ]);
    expect(g.todo.map((r) => r.task_id)).toEqual(["early-p1", "late-p1", "p3"]);
    expect(g.waiting.map((r) => r.task_id)).toEqual(["blocked", "deferred"]);
    expect(g.closed.map((r) => r.task_id)).toEqual(["done", "failed"]);
    expect(g.triage.map((r) => r.task_id)).toEqual(["triage"]);
    expect(g.in_progress).toEqual([]);
  });

  it("sorts by priority, then the oldest first — the order task.pull claims in", () => {
    const rows = [
      row("late-p1", { priority: 1, created_at: "2026-09-20T10:00:00" }),
      row("p3", { priority: 3, created_at: "2026-09-19T00:00:00" }),
      row("early-p1", { priority: 1, created_at: "2026-09-20T09:00:00" }),
      row("p2", { priority: 2, created_at: "2026-09-20T00:00:00" }),
    ];
    expect(sortTasks(rows).map((r) => r.task_id)).toEqual(["early-p1", "late-p1", "p2", "p3"]);
    expect(kindsOf([row("a", { kind: "b" }), row("c", { kind: "a" }), row("d", { kind: "b" })])).toEqual(["a", "b"]);
  });
});

describe("attention tiles", () => {
  const rows = [
    row("t1", { status: "triage" }),
    row("t2", { status: "triage" }),
    row("bh", { status: "blocked", blocked_on: "human" }),
    row("bs", { status: "blocked", blocked_on: "sender" }),
    row("f", { status: "failed" }),
    row("soon", { status: "in_progress", owner: "bot", lease_until: ago(-8) }),
    row("later", { status: "in_progress", owner: "bot", lease_until: ago(-25) }),
    row("expired", { status: "in_progress", owner: "bot", lease_until: ago(3) }),
  ];
  it("counts triage, blocked on a person, failed, and leases ending within ten minutes", () => {
    expect(attentionCounts(rows, NOW)).toEqual({ triage: 2, human: 1, failed: 1, soon: 2 });
  });
  it("lease ending soon: in progress with ten minutes or less left, an expired lease included, a lease-less row not", () => {
    expect(leaseMinutesLeft(ago(-8), NOW)).toBe(8);
    expect(leaseMinutesLeft(ago(3), NOW)).toBe(0);
    expect(leaseMinutesLeft(null, NOW)).toBeNull();
    expect(leaseEndingSoon(row("a", { status: "in_progress", lease_until: ago(-10) }), NOW)).toBe(true);
    expect(leaseEndingSoon(row("a", { status: "in_progress", lease_until: ago(-11) }), NOW)).toBe(false);
    expect(leaseEndingSoon(row("a", { status: "in_progress", lease_until: null }), NOW)).toBe(false);
    expect(leaseEndingSoon(row("a", { status: "todo", lease_until: ago(-2) }), NOW)).toBe(false);
    expect(matchesAttention(row("a", { status: "blocked", blocked_on: "external" }), "human", NOW)).toBe(false);
  });
});

describe("filters", () => {
  const owner = (inbox: string) => (inbox === "private-bot" ? "reporter" : undefined);
  const mine = row("mine", { from_agent: "reporter", from_subject: "sa:reporter-9f8e7d" });
  const theirs = row("theirs");
  const human = row("human", { from_agent: "reporter", from_subject: "alice@example.com" });
  const closed = row("closed", { status: "wont_do" });
  const failed = row("failed", { status: "failed" });
  const f = (extra: Partial<Filters>): Filters => ({
    attention: null,
    kind: null,
    poster: null,
    q: "",
    ...extra,
  });

  it("tells the inbox's own agent from another agent and from a person", () => {
    expect(posterKind(mine, "reporter")).toBe("me");
    expect(posterKind(theirs, "reporter")).toBe("bot");
    expect(posterKind(human, "reporter")).toBe("human");
    expect(posterKind(mine, undefined)).toBe("bot");
  });
  it("posted-by filters on that; closed rows pass (their lane folds them), the Failed tile keeps only failures", () => {
    expect(passesFilters(mine, f({ poster: "me" }), owner, NOW)).toBe(true);
    expect(passesFilters(theirs, f({ poster: "me" }), owner, NOW)).toBe(false);
    expect(passesFilters(theirs, f({ poster: "others" }), owner, NOW)).toBe(true);
    expect(passesFilters(closed, f({}), owner, NOW)).toBe(true);
    expect(passesFilters(failed, f({ attention: "failed" }), owner, NOW)).toBe(true);
    expect(passesFilters(closed, f({ attention: "failed" }), owner, NOW)).toBe(false);
    expect(passesFilters(theirs, f({ kind: "coverage" }), owner, NOW)).toBe(false);
  });
  it("the search matches the title, kind, poster, reason or id, case-insensitively, every word", () => {
    const r = row("0f3a9c-task", { title: "Recall dropped", kind: "bug_report", reason: "needs a repro" });
    for (const q of ["recall", "BUG_REPORT", "reporter-public", "repro", "0f3a9c", "recall bug"])
      expect(passesFilters(r, f({ q }), owner, NOW)).toBe(true);
    expect(passesFilters(r, f({ q: "recall latency" }), owner, NOW)).toBe(false);
    expect(passesFilters(r, f({ q: "   " }), owner, NOW)).toBe(true);
  });
  it("says what is in force, for the filtered state", () => {
    expect(filterSummary(f({}))).toBe("");
    expect(filterSummary(f({ q: " recall ", kind: "docs", poster: "others", attention: "triage" }))).toBe(
      '"recall" · kind:docs · posted by others · Triage · decide',
    );
  });
});

describe("the row's assignee", () => {
  it("is the owner once claimed, else the inbox's agent, else nobody", () => {
    expect(assigneeOf(row("a", { status: "in_progress", owner: "bench-bot" }), "reporter")).toEqual({
      name: "bench-bot",
      label: "Claimed by bench-bot",
    });
    expect(assigneeOf(row("a"), "reporter")).toEqual({ name: "reporter", label: "For reporter, the inbox's agent" });
    expect(assigneeOf(row("a"), undefined)).toBeNull();
  });
});

describe("what an action says when it is done", () => {
  it("is the past tense with the task's title", () => {
    expect(actionDone("accept", "Fix it")).toBe("Accepted “Fix it”: it moved to To do");
    expect(actionDone("wont_do", "Fix it")).toBe("Closed “Fix it” as won't do");
    expect(actionDone("start", "Fix it")).toBe("Started “Fix it”");
  });
});

describe("the row's one line, per state", () => {
  it("says the right thing for each status", () => {
    // The time is the row's meta; the line says who posted it.
    expect(sublineOf(row("a", { status: "triage", created_at: ago(42) }), NOW)).toEqual({
      text: "from reporter-public",
    });
    expect(sublineOf(row("a", { status: "todo" }), NOW)).toEqual({ text: "from reporter-public" });
    expect(sublineOf(row("a", { status: "todo", not_before: ago(-14 * 60) }), NOW)).toEqual({
      text: "from reporter-public",
      suffix: "· scheduled in 14h",
    });
    expect(sublineOf(row("a", { status: "todo", not_before: ago(5) }), NOW)).toEqual({ text: "from reporter-public" });
    expect(sublineOf(row("a", { status: "in_progress" }), NOW)).toBeNull();
    expect(sublineOf(row("a", { status: "in_progress" }), NOW, "seed 1/3 clean")).toEqual({
      text: "seed 1/3 clean",
      italic: true,
    });
    expect(sublineOf(row("a", { status: "blocked", blocked_on: "sender", reason: "need the flags" }), NOW)).toEqual({
      text: "blocked on sender: need the flags",
      italic: true,
    });
    expect(sublineOf(row("a", { status: "deferred", reason: "binary not built", resume_at: ago(-120) }), NOW)).toEqual({
      text: "binary not built",
      italic: true,
      suffix: "· resumes in 2h",
    });
    expect(
      sublineOf(
        row("a", {
          status: "done",
          outcome: { verdict: "confirmed", issue_url: "https://github.com/ClickHouse/example-private/issues/4121" },
        }),
        NOW,
      ),
    ).toEqual({ text: "done ·", href: "https://github.com/ClickHouse/example-private/issues/4121" });
    expect(sublineOf(row("a", { status: "done", outcome: {} }), NOW)).toBeNull();
    expect(sublineOf(row("a", { status: "wont_do", reason: "duplicate of private#3990" }), NOW)).toEqual({
      text: "duplicate of private#3990",
      italic: true,
    });
    expect(sublineOf(row("a", { status: "failed", reason: "no good build" }), NOW)).toEqual({
      text: "no good build",
      italic: true,
    });
  });
  // One date rule with every list (lib/format.ts listTime), plus the countdowns it has no form for.
  it("reads relative time the way every list says it", () => {
    expect(rel(ago(0), NOW)).toBe("just now");
    expect(rel(ago(42), NOW)).toBe("42m ago");
    expect(rel(ago(-5), NOW)).toBe("in 5m");
    expect(rel(ago(3 * 60), NOW)).toBe("3h ago");
    expect(rel(ago(-2 * 60), NOW)).toBe("in 2h");
    expect(rel(ago(3 * 24 * 60), NOW)).toBe("3d ago");
    expect(rel(ago(-3 * 24 * 60), NOW)).toBe("in 3d");
    // Past a week, the date listTime writes ("Sep 11", with the year when it is not this one).
    expect(rel(ago(9 * 24 * 60), NOW)).toBe(listTime(ago(9 * 24 * 60)));
    expect(rel(ago(-9 * 24 * 60), NOW)).toBe(`on ${listTime(ago(-9 * 24 * 60))}`);
    expect(rel(null, NOW)).toBe("");
  });
  it("names the poster and draws initials", () => {
    expect(posterName(row("a"))).toBe("reporter-public");
    expect(posterName(row("a", { from_subject: "alice@example.com" }))).toBe("alice@example.com");
    expect(initials("reporter-public")).toBe("rp");
    expect(initials("reporter")).toBe("re");
    expect(initials("jane.doe@example.com")).toBe("jd");
    expect(initials("sa:fuzz_campaign-1a2b3c")).toBe("fc");
  });
  it("picks the latest progress note from a history", () => {
    const ev = (seq: number, event: string, note = ""): TaskEvent => ({
      seq,
      task_id: "t",
      inbox: "i",
      event,
      from_status: "",
      to_status: "",
      actor: "",
      agent: "",
      fence: 0,
      version: 0,
      reason: "",
      patch: {},
      note,
      ts: "",
    });
    expect(
      latestProgressNote([
        ev(1, "claimed"),
        ev(2, "progress", "first"),
        ev(4, "progress", "second"),
        ev(3, "heartbeat"),
      ]),
    ).toBe("second");
    expect(latestProgressNote([ev(1, "claimed")])).toBe("");
    expect(firstUrl({ a: "x", b: ["nope", "https://x.y/z"] })).toBe("https://x.y/z");
  });
});

describe("actions per status (the handler's state machine)", () => {
  it("offers accept and won't-do in triage, start/defer/block/won't-do in todo, the owner's finish in progress, resume when deferred, reopen when closed short of done", () => {
    expect(actionsFor("triage")).toEqual(expect.arrayContaining(["accept", "wont_do"]));
    expect(actionsFor("todo")).toEqual(
      expect.arrayContaining(["start", "defer", "block", "wont_do", "cancel", "reassign", "edit"]),
    );
    expect(actionsFor("blocked")).toContain("unblock");
    expect(actionsFor("blocked")).not.toContain("defer");
    expect(actionsFor("deferred")).toContain("resume");
    expect(actionsFor("in_progress")).toEqual(["complete", "fail", "block", "defer", "wont_do", "cancel"]);
    expect(actionsFor("failed")).toEqual(["reopen"]);
    expect(actionsFor("wont_do")).toEqual(["reopen"]);
    expect(actionsFor("cancelled")).toEqual(["reopen"]);
    expect(actionsFor("done")).toEqual([]);
  });
});

describe("a drop on a board column", () => {
  it("names the one-step move the state machine has, and none where it has none", () => {
    expect(dropAction("triage", "todo")).toBe("accept");
    expect(dropAction("todo", "in_progress")).toBe("start");
    expect(dropAction("todo", "waiting")).toBe("block");
    expect(dropAction("in_progress", "waiting")).toBe("block");
    expect(dropAction("in_progress", "closed")).toBe("complete");
    expect(dropAction("todo", "closed")).toBe("wont_do");
    expect(dropAction("blocked", "todo")).toBe("unblock");
    expect(dropAction("deferred", "todo")).toBe("resume");
    expect(dropAction("failed", "todo")).toBe("reopen");
    // No such transition: into triage, in progress back to the queue, a jump, the own group, done.
    expect(dropAction("todo", "triage")).toBeNull();
    expect(dropAction("in_progress", "todo")).toBeNull();
    expect(dropAction("triage", "in_progress")).toBeNull();
    expect(dropAction("blocked", "waiting")).toBeNull();
    expect(dropAction("done", "todo")).toBeNull();
  });
});

describe("the brief as a document", () => {
  it("splits objective, criteria, inputs (flattened) and constraints", () => {
    const b = briefSections({
      objective: "Confirm and file privately.",
      acceptance_criteria: ["Private issue filed", "Verdict recorded"],
      constraints: "Confidential.",
      inputs: {
        pr: "ClickHouse/ClickHouse#93412",
        span: "src/Parsers/ASTJSONReadHelpers.cpp:87",
        test: "03011_json_read_expr_child_ub.sql",
      },
      spans: ["a.cpp:1"],
      finding_id: "f-77",
    });
    expect(b.objective).toBe("Confirm and file privately.");
    expect(b.criteria).toEqual(["Private issue filed", "Verdict recorded"]);
    expect(b.constraints).toBe("Confidential.");
    expect(b.inputs.map(([k]) => k)).toEqual(["pr", "span", "test", "finding_id", "spans"]);
    expect(briefSections(null)).toEqual({ objective: "", criteria: [], inputs: [], constraints: "" });
  });
  it("tells links, issue refs, spans, files and SHAs apart", () => {
    expect(inputKind("https://github.com/x/y")).toBe("url");
    expect(inputKind("#120507")).toBe("issue");
    expect(inputKind("ClickHouse/ClickHouse#93412")).toBe("issue");
    expect(issueHref("ClickHouse/ClickHouse#93412")).toBe("https://github.com/ClickHouse/ClickHouse/issues/93412");
    expect(issueHref("#120507")).toBeNull();
    expect(inputKind("src/Parsers/ASTJSONReadHelpers.cpp:87")).toBe("span");
    expect(inputKind("03011_json_read_expr_child_ub.sql")).toBe("file");
    expect(inputKind("run.sh")).toBe("file");
    expect(inputKind("9f3c1e2")).toBe("hex");
    expect(inputKind("exception 36")).toBe("text");
    expect(inputKind(42)).toBe("text");
  });
});

describe("the poster's record and the rule", () => {
  it("tallies done / won't do / failed / open and leaves cancelled out of open", () => {
    const r = tallyRecord(
      [
        row("a", { status: "done" }),
        row("b", { status: "done" }),
        row("c", { status: "wont_do" }),
        row("d", { status: "triage" }),
        row("e", { status: "cancelled" }),
        row("f", { status: "in_progress" }),
      ],
      true,
    );
    expect(r).toEqual({ done: 2, wont_do: 1, failed: 0, open: 2, total: 6, truncated: true });
  });
  it("resolves the rule by subject, then agent, then *, exact kind first; checks the evidence the rule requires", () => {
    const pol = (
      poster: string,
      kind: string,
      mode: PosterPolicy["mode"],
      require_evidence: string[] = [],
    ): PosterPolicy => ({
      org: "dev",
      inbox: "i",
      poster,
      kind,
      mode,
      require_evidence,
      set_by: "",
      set_at: "",
      version: 1,
    });
    const policies = [
      pol("*", "*", "deny"),
      pol("sa:x", "*", "review", ["inputs.finding_id"]),
      pol("sa:x", "bug", "auto"),
    ];
    expect(resolvePolicy(policies, { from_subject: "sa:x", from_agent: "x", kind: "bug" })?.mode).toBe("auto");
    expect(resolvePolicy(policies, { from_subject: "sa:x", from_agent: "x", kind: "other" })?.mode).toBe("review");
    expect(resolvePolicy(policies, { from_subject: "sa:y", from_agent: "y", kind: "bug" })?.mode).toBe("deny");
    expect(resolvePolicy([], { from_subject: "sa:y", from_agent: "y", kind: "bug" })).toBeNull();
    expect(evidenceStatus(["inputs.finding_id", "repro"], { inputs: { finding_id: "f-77" } })).toEqual({
      present: ["inputs.finding_id"],
      missing: ["repro"],
    });
    expect(evidenceStatus(["finding_id"], { inputs: { finding_id: "f-77" } })).toEqual({
      present: ["finding_id"],
      missing: [],
    });
  });

  it("walks objects only: an array along a dotted path reads as absent", () => {
    expect(evidenceStatus(["inputs.files.0", "files.0"], { inputs: { files: ["a.log"] } })).toEqual({
      present: [],
      missing: ["inputs.files.0", "files.0"],
    });
    expect(evidenceStatus(["0"], { inputs: ["a.log"] })).toEqual({ present: [], missing: ["0"] });
  });
});

describe("the timeline", () => {
  const ev = (seq: number, event: string, actor = ""): TaskEvent => ({
    seq,
    task_id: "t",
    inbox: "i",
    event,
    from_status: "",
    to_status: "",
    actor,
    agent: "",
    fence: 0,
    version: 0,
    reason: "",
    patch: {},
    note: "",
    ts: `2026-09-20T00:00:0${seq}`,
  });
  it("collapses a run of heartbeats into one line with a count", () => {
    const out = collapseHeartbeats([
      ev(1, "claimed"),
      ev(2, "heartbeat"),
      ev(3, "heartbeat"),
      ev(4, "heartbeat"),
      ev(5, "progress"),
      ev(6, "heartbeat"),
    ]);
    expect(out.map((e) => [e.event, e.collapsed ?? 1])).toEqual([
      ["claimed", 1],
      ["heartbeat", 3],
      ["progress", 1],
      ["heartbeat", 1],
    ]);
    expect(out[1].ts).toBe("2026-09-20T00:00:04");
  });
  it("tells a person, the sweeper and an agent apart", () => {
    expect(actorKind("human:alice@example.com")).toBe("human");
    expect(actorKind("alice@example.com")).toBe("human");
    expect(actorKind("process:task_lease_sweep")).toBe("sweeper");
    expect(actorKind("process:reporter")).toBe("bot");
    expect(actorKind("", "reporter")).toBe("bot");
  });
});

describe("the bell", () => {
  const counts: InboxCount[] = [
    { inbox: "a", status: "triage", blocked_on: "", n: 2, oldest: "2026-09-20T01:00:00" },
    { inbox: "a", status: "todo", blocked_on: "", n: 9, oldest: "2026-09-20T02:00:00" },
    { inbox: "b", status: "blocked", blocked_on: "human", n: 1, oldest: "2026-09-19T00:00:00" },
    { inbox: "b", status: "blocked", blocked_on: "sender", n: 4, oldest: "2026-09-18T00:00:00" },
    { inbox: "c", status: "done", blocked_on: "", n: 40, oldest: null },
    { inbox: "d", status: "blocked", blocked_on: "external", n: 2, oldest: "2026-09-17T00:00:00" },
  ];
  it("counts triage and blocked-on-a-person, never the queue, the finished, or a block on the sender", () => {
    expect(bellCount(counts)).toBe(3);
    expect(bellCount([])).toBe(0);
  });
  it("lists the inboxes with something waiting on a person, splitting the blocks, oldest among those", () => {
    expect(attentionByInbox(counts)).toEqual([
      { inbox: "a", triage: 2, blockedHuman: 0, blockedOther: 0, oldest: "2026-09-20T01:00:00" },
      { inbox: "b", triage: 0, blockedHuman: 1, blockedOther: 4, oldest: "2026-09-19T00:00:00" },
    ]);
  });
});

describe("task.update conflict", () => {
  it("reads current_version and the current row from a 409 PRECONDITION_FAILED", () => {
    const err = new ApiError({
      status: 409,
      code: "PRECONDITION_FAILED",
      message: "task changed",
      detail: { current_version: 42, current: { title: "theirs" } },
    });
    expect(conflictOf(err)).toEqual({ currentVersion: 42, current: { title: "theirs" } });
  });
  it("keeps only the current row's fields that have the right type", () => {
    const err = new ApiError({
      status: 409,
      code: "PRECONDITION_FAILED",
      detail: {
        current_version: 7,
        current: { title: 5, brief: { goal: "x" }, status: "not_a_status", last_actor: "human:ada", extra: 1 },
      },
    });
    expect(conflictOf(err)).toEqual({ currentVersion: 7, current: { brief: { goal: "x" }, last_actor: "human:ada" } });
    const listBrief = new ApiError({
      status: 409,
      code: "PRECONDITION_FAILED",
      detail: { current_version: 8, current: { brief: ["a"], status: "blocked" } },
    });
    expect(conflictOf(listBrief)).toEqual({ currentVersion: 8, current: { status: "blocked" } });
  });
  it("is null for any other failure", () => {
    expect(conflictOf(new ApiError({ status: 409, code: "TASK_NOT_EDITABLE" }))).toBeNull();
    expect(conflictOf(new ApiError({ status: 403, code: "AUTH_INSUFFICIENT" }))).toBeNull();
    expect(conflictOf(new Error("boom"))).toBeNull();
  });
});

describe("time helpers", () => {
  it("round-trips a datetime-local value through ISO", () => {
    const local = "2026-09-20T14:30";
    const iso = must(isoFromLocal(local), "ISO from local");
    expect(iso.endsWith("Z")).toBe(true);
    expect(localFromIso(iso)).toBe(local);
    expect(isoFromLocal("")).toBeNull();
    expect(localFromIso(null)).toBe("");
  });
});

// The page reads times with parseChTime, as every list does: Safari's Date.parse refuses a
// ClickHouse time ("2026-09-20 01:00:00", or six fraction digits). Date.parse is held to the
// ISO form Safari accepts.
describe("ClickHouse times under a strict date parser", () => {
  afterEach(() => vi.restoreAllMocks());
  it("reads a lease, a schedule and a date input from a ClickHouse time", () => {
    const parse = Date.parse;
    vi.spyOn(Date, "parse").mockImplementation((s: string) =>
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?(Z|[+-]\d{2}:\d{2})$/.test(s) ? parse(s) : NaN,
    );
    const now = parse("2026-09-20T01:00:00Z");
    expect(leaseMinutesLeft("2026-09-20 01:08:00.123456", now)).toBe(8);
    expect(rel("2026-09-20 00:18:00", now)).toBe("42m ago");
    expect(localFromIso("2026-09-20 01:00:00")).not.toBe("");
  });
});
