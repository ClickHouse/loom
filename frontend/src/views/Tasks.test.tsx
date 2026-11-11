import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import type { Inbox, InboxCount, TaskListRow, TaskRow } from "../api/tasks";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
import { must } from "../test/must";
import { Tasks } from "./Tasks";

// The Tasks page. Mocked at the wrapper level, like Recent.polls.test.tsx: the page reads
// inboxes, counts and the picked inbox's tasks once each, groups rows by what happens
// next, counts what needs a person in four tiles, clears a triage row inline, and opens a
// document-shaped panel whose footer offers only the status's actions.

const INBOX: Inbox = {
  org: "dev",
  inbox: "private-bot",
  owner_agent: "reporter",
  owner_namespace: "team",
  description: "the private bot's queue",
  posters: ["sa:reporter-public-1a2b3c"],
  accept_disclosure: ["confidential", "internal", "public_ok"],
  default_lease_seconds: 1800,
  max_attempts: 3,
  max_defers: 5,
  triage_escalate_after_seconds: 14400,
  webhook_url: "",
  created_by: "",
  created_at: "2026-09-20T00:00:00",
  updated_at: "2026-09-20T00:00:00",
  version: 1,
};
const INBOX2: Inbox = { ...INBOX, inbox: "public-bot", owner_agent: "reporter-public" };

const iso = (minAgo: number) => new Date(Date.now() - minAgo * 60_000).toISOString().replace(/\.\d+Z$/, "");

const task = (id: string, extra: Partial<TaskListRow>): TaskListRow => ({
  task_id: id,
  inbox: INBOX.inbox,
  kind: "bug_report",
  priority: 3,
  status: "todo",
  reason: "",
  blocked_on: "",
  not_before: null,
  resume_at: null,
  deadline: null,
  defer_count: 0,
  title: `Task ${id}`,
  from_agent: "reporter-public",
  from_subject: "sa:reporter-public-1a2b3c",
  owner: "",
  lease_until: null,
  parent_task_id: null,
  disclosure: "internal",
  created_at: iso(60),
  updated_at: iso(60),
  version: 1,
  ...extra,
});

const ROWS: TaskListRow[] = [
  task("low-late", {
    priority: 4,
    created_at: iso(10),
    kind: "coverage_gap",
    from_agent: "reporter",
    from_subject: "sa:reporter-9f8e7d",
  }),
  task("urgent", { priority: 1, created_at: iso(42), status: "triage" }),
  task("blocked", { priority: 2, status: "blocked", blocked_on: "human", reason: "needs a repro" }),
  task("running", { priority: 2, status: "in_progress", owner: "reporter", lease_until: iso(-6) }),
  task("closed", { priority: 2, status: "wont_do", reason: "duplicate of #12" }),
  task("failed", { priority: 3, status: "failed", reason: "no good build" }),
];

const COUNTS: InboxCount[] = [
  { inbox: "private-bot", status: "todo", blocked_on: "", n: 1, oldest: null },
  { inbox: "private-bot", status: "in_progress", blocked_on: "", n: 1, oldest: null },
  { inbox: "public-bot", status: "todo", blocked_on: "", n: 4, oldest: null },
];

const full = (r: TaskListRow, extra: Partial<TaskRow> = {}): TaskRow => ({
  ...r,
  org: "dev",
  namespace: "team",
  brief: {
    objective: "Fix it",
    acceptance_criteria: ["a test"],
    inputs: { pr: "ClickHouse/ClickHouse#93412" },
    constraints: "Confidential.",
  },
  outcome: {},
  origin: "",
  attempt: 0,
  fence: 0,
  last_actor: "",
  ...extra,
});

vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    listInboxes: vi.fn(async () => [INBOX]),
    listTasks: vi.fn(async () => ({ tasks: ROWS, truncated: false })),
    taskCounts: vi.fn(async () => COUNTS),
    getTask: vi.fn(async () => null),
    taskHistory: vi.fn(async () => []),
    listPolicies: vi.fn(async () => []),
    acceptTask: vi.fn(async () => ({ task: full({ ...ROWS[1], status: "todo" }), seq: 1 })),
    wontDoTask: vi.fn(async (_id: unknown, taskId: string, reason: string) => ({
      task: full({ ...ROWS[1], task_id: taskId, status: "wont_do", reason }),
      seq: 2,
    })),
    resumeTask: vi.fn(async () => ({ task: full({ ...ROWS[0], status: "todo" }), seq: 3 })),
    claimTask: vi.fn(async () => ({ task: full({ ...ROWS[0], status: "in_progress", owner: "loom-ui" }), seq: 4 })),
  };
});
import {
  acceptTask,
  claimTask,
  getTask,
  listInboxes,
  listPolicies,
  listTasks,
  taskCounts,
  taskHistory,
  wontDoTask,
} from "../api/tasks";

const admin: WhoAmI = {
  authenticated: true,
  subject: "op@example.com",
  org: "dev",
  roles: ["ADMIN"],
  namespaces: ["team"],
};
const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team"],
};
const row = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind: "memory",
});

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[]) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    view: "tasks",
  });
}

// Task rows only: each group's first row is its header.
const taskRows = (el: HTMLElement = document.body) =>
  within(el)
    .queryAllByRole("row")
    .filter((r) => r.hasAttribute("data-task-id"))
    .map((r) => r.getAttribute("aria-label"));
const groupRows = (name: RegExp) => taskRows(screen.getByRole("rowgroup", { name }));
const laneHead = (name: RegExp) => within(screen.getByRole("rowgroup", { name })).getByRole("button", { name });

// Click UI's Select items carry no option role; they are addressed by value.
async function pick(user: ReturnType<typeof userEvent.setup>, select: string, value: string) {
  await user.click(screen.getByRole("button", { name: select }));
  await user.click(must(document.querySelector<HTMLElement>(`[cui-select-item][data-value="${value}"]`), value));
}

describe("Tasks page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    localStorage.clear();
    window.history.replaceState(null, "", "/tasks");
    // Defaults again: a test that sets a lasting implementation must not leak.
    vi.mocked(listInboxes).mockImplementation(async () => [INBOX]);
    vi.mocked(listTasks).mockImplementation(async () => ({ tasks: ROWS, truncated: false }));
    vi.mocked(taskCounts).mockImplementation(async () => COUNTS);
    vi.mocked(getTask).mockImplementation(async () => null);
    vi.mocked(listPolicies).mockImplementation(async () => []);
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("reads the inboxes, the counts and the picked inbox's tasks once each, with the list projection, and writes ?inbox=", async () => {
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1));
    await screen.findByRole("row", { name: "Task urgent" });
    expect(listInboxes).toHaveBeenCalledTimes(1);
    expect(taskCounts).toHaveBeenCalledTimes(1);
    expect(vi.mocked(taskCounts).mock.calls[0][0]).toEqual({ org: "dev", namespace: "team" });
    const [caller, opts] = vi.mocked(listTasks).mock.calls[0];
    expect(caller).toEqual({ org: "dev", namespace: "team" });
    expect(opts?.inbox).toBe("private-bot");
    expect(opts?.fields ?? "default").toBe("default"); // the wrapper fills TASKS_LIST_FIELDS
    expect(new URLSearchParams(window.location.search).get("inbox")).toBe("private-bot");
    expect(listTasks).toHaveBeenCalledTimes(1);
  });

  it("writes ?inbox= and ?task= into the entry without dropping the place it remembers", async () => {
    const place = { scrollTop: 120, focusKey: "task:urgent", inspected: null, inspectedAgent: "" };
    window.history.replaceState({ loomPlace: place }, "", "/tasks");
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(new URLSearchParams(window.location.search).get("inbox")).toBe("private-bot");
    expect((window.history.state as Record<string, unknown>).loomPlace).toEqual(place);
    fireEvent.click(screen.getByRole("row", { name: "Task urgent" }));
    await waitFor(() => expect(new URLSearchParams(window.location.search).get("task")).toBe("urgent"));
    expect((window.history.state as Record<string, unknown>).loomPlace).toEqual(place);
  });

  it("the list is a valid grid: rowgroups of rows of gridcells, axe-clean with every group open", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(laneHead(/^Closed/));
    const grid = screen.getByRole("grid", { name: "Tasks by what happens next" });
    expect(await axeViolations(grid)).toEqual([]);
  });

  it("groups rows by what happens next, in queue order, with Closed folded until opened", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(groupRows(/^Triage/)).toEqual(["Task urgent"]);
    expect(groupRows(/^To do/)).toEqual(["Task low-late"]);
    expect(groupRows(/^In progress/)).toEqual(["Task running"]);
    expect(groupRows(/^Waiting/)).toEqual(["Task blocked"]);
    expect(screen.queryByRole("checkbox", { name: "Show closed" })).toBeNull();
    expect(screen.getByText("decide: accept or won't do")).toBeInTheDocument();
    // The row's second line: the kind as a plain badge, then the reason for a wait, the poster
    // in triage, the lease in progress. The time and the assignee are the row's meta.
    const urgent = screen.getByRole("row", { name: "Task urgent" });
    expect(within(urgent).getByText("from reporter-public")).toBeInTheDocument();
    expect(within(urgent).getByText("bug_report")).toBeInTheDocument();
    expect(within(urgent).getByText("42m ago").closest("time")).toHaveAttribute("datetime");
    expect(within(urgent).getByText("P1")).toBeInTheDocument();
    expect(within(urgent).getByText("For reporter, the inbox's agent")).toBeInTheDocument();
    expect(screen.getByText("blocked on human: needs a repro")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Lease: 6 min left" })).toBeInTheDocument();
    expect(
      within(screen.getByRole("row", { name: "Task running" })).getByText("Claimed by reporter"),
    ).toBeInTheDocument();
    // Closed is listed with its count and folded: the header shows, the rows do not.
    const closed = screen.getByRole("rowgroup", { name: /^Closed/ });
    expect(laneHead(/^Closed/)).toHaveAttribute("aria-expanded", "false");
    expect(laneHead(/^Closed/)).toHaveTextContent("2");
    expect(groupRows(/^Closed/)).toEqual([]);
    await user.click(laneHead(/^Closed/));
    expect(groupRows(/^Closed/)).toEqual(["Task closed", "Task failed"]);
    expect(within(closed).getByText("duplicate of #12")).toBeInTheDocument();
  });

  it("counts what needs a person in four tiles, from the loaded rows, and filters on a click", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(taskCounts).toHaveBeenCalledTimes(1); // the tiles cost no extra call
    const tiles = screen.getByRole("group", { name: "Needs you" });
    expect(within(tiles).getByRole("button", { name: "Triage · decide: 1" })).toBeInTheDocument();
    expect(within(tiles).getByRole("button", { name: "Blocked on a person: 1" })).toBeInTheDocument();
    expect(within(tiles).getByRole("button", { name: "Failed · reopen or close: 1" })).toBeInTheDocument();
    expect(within(tiles).getByRole("button", { name: "Lease ending ≤10 min: 1" })).toBeInTheDocument();
    await user.click(within(tiles).getByRole("button", { name: "Failed · reopen or close: 1" }));
    // a closed row the tile asks for shows even with Show closed off
    expect(taskRows()).toEqual(["Task failed"]);
    await user.click(within(tiles).getByRole("button", { name: "Failed · reopen or close: 1" })); // off again
    await user.click(within(tiles).getByRole("button", { name: "Lease ending ≤10 min: 1" }));
    expect(taskRows()).toEqual(["Task running"]);
  });

  it("the toolbar searches and filters by kind and by who posted (the inbox's own agent vs others), and counts", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    const bar = screen.getByLabelText("Search and filters");
    expect(bar).toHaveTextContent("6 tasks");
    await pick(user, "Kind", "coverage_gap");
    expect(taskRows()).toEqual(["Task low-late"]);
    expect(bar).toHaveTextContent("1 match");
    await pick(user, "Kind", "*");
    await pick(user, "Posted by", "me");
    expect(taskRows()).toEqual(["Task low-late"]);
    await pick(user, "Posted by", "others");
    expect(taskRows()).toEqual(["Task urgent", "Task running", "Task blocked", "Task closed", "Task failed"]);
    await pick(user, "Posted by", "*");
    await user.type(within(bar).getByLabelText("Search tasks"), "repro");
    expect(taskRows()).toEqual(["Task blocked"]);
  });

  it("a search that matches nothing says so on one line, and Clear filters brings the rows back", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.type(screen.getByLabelText("Search tasks"), "zzz");
    expect(screen.getByRole("status")).toHaveTextContent('No tasks match "zzz"');
    expect(screen.queryByRole("grid")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("row", { name: "Task urgent" })).toBeInTheDocument();
    expect(screen.getByLabelText("Search tasks")).toHaveValue("");
  });

  it("/ focuses the search", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.keyboard("/");
    expect(screen.getByLabelText("Search tasks")).toHaveFocus();
  });

  it("a failed read says so with Retry, and the tiles show no zero", async () => {
    vi.mocked(listTasks).mockRejectedValueOnce(new Error("ClickHouse timed out"));
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("heading", { name: "Couldn't load tasks" });
    const tiles = screen.getByRole("group", { name: "Needs you" });
    expect(within(tiles).queryByText("0")).toBeNull();
    expect(within(tiles).getByRole("button", { name: "Triage · decide: not loaded" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("row", { name: "Task urgent" })).toBeInTheDocument();
    expect(listTasks).toHaveBeenCalledTimes(2);
  });

  it("an inbox with only closed tasks says nothing is open and offers to show them", async () => {
    vi.mocked(listTasks).mockResolvedValue({ tasks: [ROWS[4], ROWS[5]], truncated: false });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("heading", { name: "No open tasks" });
    await user.click(screen.getByRole("button", { name: "Show closed" }));
    expect(groupRows(/^Closed/)).toEqual(["Task closed", "Task failed"]);
  });

  it("on a phone with Board saved, an inbox of closed tasks still says nothing is open", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: /max-width: (639|767|1279)px/.test(query),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    localStorage.setItem("loom_tasks_mode", "board");
    try {
      vi.mocked(listTasks).mockResolvedValue({ tasks: [ROWS[4], ROWS[5]], truncated: false });
      seed(admin, [row("admin")]);
      render(<Tasks />);
      expect(await screen.findByRole("heading", { name: "No open tasks" })).toBeInTheDocument();
    } finally {
      localStorage.removeItem("loom_tasks_mode");
      vi.unstubAllGlobals();
    }
  });

  it("a lane shows 20 rows, then Show all N opens the rest and focuses the first new one", async () => {
    const many = Array.from({ length: 25 }, (_, i) =>
      task(`t${String(i).padStart(2, "0")}`, { created_at: iso(100 - i) }),
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: many, truncated: false });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task t00" });
    expect(groupRows(/^To do/)).toHaveLength(20);
    await user.click(screen.getByRole("button", { name: "Show all 25" }));
    expect(groupRows(/^To do/)).toHaveLength(25);
    expect(screen.getByRole("row", { name: "Task t20" })).toHaveFocus();
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });

  it("Show all N leaves the task open in the panel where it is", async () => {
    const many = Array.from({ length: 25 }, (_, i) =>
      task(`t${String(i).padStart(2, "0")}`, { created_at: iso(100 - i) }),
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: many, truncated: false });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await user.click(await screen.findByRole("row", { name: "Task t03" }));
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    expect(within(panel).getByRole("heading", { name: "Task t03" })).toBeInTheDocument();
    const before = window.location.search;
    await user.click(screen.getByRole("button", { name: "Show all 25" }));
    expect(screen.getByRole("row", { name: "Task t20" })).toHaveFocus();
    // The panel still holds t03: Show all moved focus, it opened nothing.
    expect(window.location.search).toBe(before);
    const still = screen.getByRole("complementary", { name: "Task detail" });
    expect(within(still).getByRole("heading", { name: "Task t03" })).toBeInTheDocument();
  });

  it("on the board, Show all N focuses the first new card and leaves the open task where it is", async () => {
    const many = Array.from({ length: 25 }, (_, i) =>
      task(`t${String(i).padStart(2, "0")}`, { created_at: iso(100 - i) }),
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: many, truncated: false });
    localStorage.setItem("loom_tasks_mode", "board");
    try {
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("region", { name: "Tasks board" });
      await user.click(document.querySelector<HTMLElement>('[data-task-id="t03"]') as HTMLElement);
      const panel = await screen.findByRole("complementary", { name: "Task detail" });
      expect(within(panel).getByRole("heading", { name: "Task t03" })).toBeInTheDocument();
      const before = window.location.search;
      await user.click(screen.getByRole("button", { name: "Show all 25" }));
      await waitFor(() => expect(document.querySelector('[data-task-id="t20"]')).toHaveFocus());
      expect(window.location.search).toBe(before);
      const still = screen.getByRole("complementary", { name: "Task detail" });
      expect(within(still).getByRole("heading", { name: "Task t03" })).toBeInTheDocument();
    } finally {
      localStorage.removeItem("loom_tasks_mode");
    }
  });

  it("says its count covers the first 200 tasks when an inbox holds more", async () => {
    vi.mocked(listTasks).mockResolvedValue({ tasks: ROWS, truncated: true });
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(screen.getByLabelText("Search and filters")).toHaveTextContent("in the first 200 per inbox");
  });

  it("every row has an actions menu with the moves its status allows; the triage row's one inline action is Accept", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    const urgent = await screen.findByRole("row", { name: "Task urgent" });
    expect(within(urgent).getByRole("button", { name: "Accept Task urgent" })).toBeInTheDocument();
    expect(within(urgent).queryByRole("button", { name: /Won't do/ })).toBeNull();
    await user.click(within(urgent).getByRole("button", { name: "Actions for Task urgent" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Won't do…",
      "Reassign…",
      "Cancel task…",
      "Copy id",
    ]);
    await user.keyboard("{Escape}");
    const todo = screen.getByRole("row", { name: "Task low-late" });
    expect(within(todo).queryByRole("button", { name: /^Accept/ })).toBeNull();
    await user.click(within(todo).getByRole("button", { name: "Actions for Task low-late" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Start",
      "Defer…",
      "Block…",
      "Won't do…",
      "Reassign…",
      "Cancel task…",
      "Copy id",
    ]);
    await user.click(screen.getByRole("menuitem", { name: "Start" }));
    await waitFor(() => expect(claimTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "low-late"));
  });

  // Picking another inbox from the bell while on Tasks changes the page, not only the URL.
  it("opens the inbox a navigation names while the page is already up", async () => {
    vi.mocked(listInboxes).mockResolvedValueOnce([INBOX, INBOX2]);
    vi.mocked(listTasks).mockImplementation(async () => ({ tasks: ROWS, truncated: false }));
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    act(() => useStore.getState().navigate("tasks", { query: { inbox: INBOX2.inbox } }));
    await waitFor(() =>
      expect(within(rail).getByRole("button", { name: /public-bot/ })).toHaveAttribute("aria-current", "true"),
    );
  });

  it("lists every inbox with its to-do and running counts, and All inboxes reads one task.list per inbox", async () => {
    vi.mocked(listInboxes).mockResolvedValueOnce([INBOX, INBOX2]);
    // Each inbox answers with its own tasks; task ids are unique across inboxes.
    vi.mocked(listTasks).mockImplementation(async (_scope, opts) => ({
      tasks:
        opts?.inbox === INBOX2.inbox
          ? ROWS.map((r) => ({ ...r, task_id: `${r.task_id}-b`, inbox: INBOX2.inbox }))
          : ROWS,
      truncated: false,
    }));
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(rail).getByRole("button", { name: /private-bot.*1 to do · 1 running/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(within(rail).getByRole("button", { name: /public-bot.*4 to do/ })).toBeInTheDocument();
    await user.click(within(rail).getByRole("button", { name: /All inboxes/ }));
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(3));
    expect(
      vi
        .mocked(listTasks)
        .mock.calls.slice(1)
        .map((c) => c[1]?.inbox)
        .sort(),
    ).toEqual(["private-bot", "public-bot"]);
    expect(new URLSearchParams(window.location.search).get("inbox")).toBe("*");
  });

  it("clears a triage row inline: Accept calls task.accept and the row moves to To do after the re-read", async () => {
    vi.mocked(listTasks)
      .mockResolvedValueOnce({ tasks: ROWS, truncated: false })
      .mockResolvedValue({
        tasks: ROWS.map((r) => (r.task_id === "urgent" ? { ...r, status: "todo" as const } : r)),
        truncated: false,
      });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "Accept Task urgent" }));
    await waitFor(() => expect(acceptTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "urgent"));
    await waitFor(() => expect(groupRows(/^To do/)).toEqual(["Task urgent", "Task low-late"]));
    expect(
      within(screen.getByRole("rowgroup", { name: /^Triage/ })).getByText("Nothing waiting for a decision."),
    ).toBeInTheDocument();
    expect(taskCounts).toHaveBeenCalledTimes(2); // the rail's numbers follow the action
    // The decision is made: the focus goes on to the next task, not back to the page.
    const next = screen.getByRole("row", { name: "Task low-late" });
    expect(next).toHaveFocus();
    expect(next).toHaveAttribute("aria-selected", "true");
  });

  it("Won't do… from the row's menu asks for a reason, calls task.wont_do and the row lands in Closed", async () => {
    vi.mocked(listTasks)
      .mockResolvedValueOnce({ tasks: ROWS, truncated: false })
      .mockResolvedValue({
        tasks: ROWS.map((r) =>
          r.task_id === "urgent" ? { ...r, status: "wont_do" as const, reason: "duplicate of private#3990" } : r,
        ),
        truncated: false,
      });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "Actions for Task urgent" }));
    await user.click(screen.getByRole("menuitem", { name: "Won't do…" }));
    const dialog = await screen.findByRole("dialog");
    // A reasoned, reversible close: the primary button, not the danger one.
    const confirm = within(dialog).getByRole("button", { name: "Won't do" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "duplicate of private#3990");
    await user.click(confirm);
    await waitFor(() =>
      expect(wontDoTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "urgent", "duplicate of private#3990"),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // The row left Triage: the focus is on the next task.
    await waitFor(() => expect(screen.getByRole("row", { name: "Task low-late" })).toHaveFocus());
    await user.click(laneHead(/^Closed/));
    await waitFor(() => expect(groupRows(/^Closed/)).toContain("Task urgent"));
    expect(screen.getByText("duplicate of private#3990")).toBeInTheDocument();
  });

  it("Back in a row's dialog returns the focus to the row", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "Actions for Task urgent" }));
    await user.click(screen.getByRole("menuitem", { name: "Won't do…" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("row", { name: "Task urgent" })).toHaveFocus();
    expect(wontDoTask).not.toHaveBeenCalled();
  });

  it("a row opens the panel, which reads task.get and task.history once; a triage task adds the poster's record and the rule", async () => {
    vi.mocked(getTask).mockResolvedValueOnce(full(ROWS[1]));
    vi.mocked(listTasks)
      .mockResolvedValueOnce({ tasks: ROWS, truncated: false })
      .mockResolvedValueOnce({
        tasks: [
          task("a", { status: "done" }),
          task("b", { status: "done" }),
          task("c", { status: "wont_do" }),
          ROWS[1],
        ],
        truncated: false,
      });
    vi.mocked(listPolicies).mockResolvedValueOnce([
      {
        org: "dev",
        inbox: "private-bot",
        poster: "sa:reporter-public-1a2b3c",
        kind: "*",
        mode: "review",
        require_evidence: ["inputs.pr"],
        set_by: "",
        set_at: "",
        version: 1,
      },
    ]);
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await user.click(await screen.findByRole("row", { name: "Task urgent" }));
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    expect(within(panel).getByRole("heading", { name: "Task urgent" })).toBeInTheDocument();
    await within(panel).findByText("Fix it");
    expect(within(panel).getByRole("heading", { name: "Acceptance criteria" })).toBeInTheDocument();
    expect(
      within(panel).getByRole("link", { name: "ClickHouse/ClickHouse#93412 (opens in a new tab)" }),
    ).toHaveAttribute("href", "https://github.com/ClickHouse/ClickHouse/issues/93412");
    expect(within(panel).getByText("Confidential.")).toBeInTheDocument();
    expect(within(panel).getByText(/from reporter-public/)).toBeInTheDocument();
    await within(panel).findByLabelText(/4 tasks from this poster: 2 done, 1 won't do, 0 failed, 1 open/);
    await within(panel).findByText(/evidence/);
    expect(getTask).toHaveBeenCalledTimes(1);
    expect(taskHistory).toHaveBeenCalledTimes(1);
    expect(listPolicies).toHaveBeenCalledTimes(1);
    expect(listTasks).toHaveBeenCalledTimes(2);
    expect(vi.mocked(listTasks).mock.calls[1][1]).toMatchObject({
      inbox: "private-bot",
      postedBy: "sa:reporter-public-1a2b3c",
      fields: ["status", "kind", "created_at"],
    });
    // the footer is the verb: Accept primary, Won't do…, the rest under the menu
    const footer = within(panel).getByLabelText("Actions");
    expect(within(footer).getByRole("button", { name: "Accept" })).toBeInTheDocument();
    expect(within(footer).getByRole("button", { name: "Won't do…" })).toBeInTheDocument();
    expect(within(footer).queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
    await user.click(within(footer).getByRole("button", { name: "More actions" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Edit brief",
      "Reassign…",
      "Cancel task…",
      "Copy id",
    ]);
    expect(new URLSearchParams(window.location.search).get("task")).toBe("urgent");
  });

  it("a viewer sees the page but no New task, settings, inline or footer actions", async () => {
    seed(viewer, [row("viewer")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(screen.queryByRole("button", { name: /New task/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept Task urgent" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Page options" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Keyboard shortcuts"]);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Actions for Task urgent" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Copy id"]);
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("row", { name: "Task urgent" }));
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    expect(within(panel).queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
  });

  it("an admin gets New task, and Inbox settings and Register inbox under Page options", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(screen.getAllByRole("button", { name: /New task/ })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Page options" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Inbox settings…",
      "Register inbox…",
      "Keyboard shortcuts",
    ]);
  });

  it("a namespace switch closes the task the old namespace had open", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.keyboard("j{Enter}");
    await screen.findByRole("complementary", { name: "Task detail" });
    act(() => useStore.getState().setIdentity({ org: "dev", namespace: "other", agent: "loom-ui" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Task detail" })).toBeNull());
    // Once the new namespace's inboxes load, nothing reads the old task under it.
    await waitFor(() =>
      expect(
        vi.mocked(listInboxes).mock.calls.some(([id]) => (id as { namespace: string }).namespace === "other"),
      ).toBe(true),
    );
    expect(screen.queryByRole("complementary", { name: "Task detail" })).toBeNull();
    expect(vi.mocked(getTask).mock.calls.filter(([id]) => (id as { namespace: string }).namespace === "other")).toEqual(
      [],
    );
  });

  it("keys: j/k move the selection, Enter opens, Esc closes, a accepts the selected triage row", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.keyboard("j");
    expect(screen.getByRole("row", { name: "Task urgent" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("j");
    expect(screen.getByRole("row", { name: "Task low-late" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("k");
    expect(screen.getByRole("row", { name: "Task urgent" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    await screen.findByRole("complementary", { name: "Task detail" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("complementary", { name: "Task detail" })).not.toBeInTheDocument();
    await user.keyboard("a");
    await waitFor(() => expect(acceptTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "urgent"));
  });

  // The keys live in the help dialog and in each action's tooltip, not in a strip under the
  // lanes; a lane's name is its state badge.
  it("names the keys in the action tooltips and the help dialog, with no key strip", async () => {
    seed(admin, [row("admin")]);
    useStore.setState({ helpOpen: false });
    const user = userEvent.setup();
    const { container } = render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(container.querySelector("kbd")).toBeNull();
    const accept = screen.getByRole("button", { name: "Accept Task urgent" });
    expect(accept).toHaveAttribute("aria-keyshortcuts", "a");
    await user.hover(accept);
    expect((await screen.findAllByText("Accept · A")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Page options" }));
    await user.click(screen.getByRole("menuitem", { name: "Keyboard shortcuts" }));
    expect(useStore.getState().helpOpen).toBe(true);
    const triage = within(screen.getByRole("rowgroup", { name: /^Triage/ })).getByRole("button", { name: /^Triage/ });
    expect(triage.querySelector("[class*='badge']")).not.toBeNull();
  });

  it("the grid is one tab stop: arrows move between headers and rows, Home / End jump, Left / Right walk a row", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    const grid = screen.getByRole("grid", { name: "Tasks by what happens next" });
    const stops = () => [...grid.querySelectorAll('[tabindex="0"]')];
    expect(stops()).toHaveLength(1);
    const triageHead = within(screen.getByRole("rowgroup", { name: /^Triage/ })).getByRole("button", {
      name: /Triage/,
    });
    expect(stops()[0]).toBe(triageHead);
    triageHead.focus();
    await user.keyboard("{ArrowDown}");
    const urgent = screen.getByRole("row", { name: "Task urgent" });
    expect(urgent).toHaveFocus();
    expect(urgent).toHaveAttribute("aria-selected", "true");
    expect(stops()).toEqual([urgent]);
    await user.keyboard("{ArrowRight}");
    expect(within(urgent).getByRole("button", { name: "Accept Task urgent" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(within(urgent).getByRole("button", { name: "Actions for Task urgent" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(urgent).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(
      within(screen.getByRole("rowgroup", { name: /^To do/ })).getByRole("button", { name: /To do/ }),
    ).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("row", { name: "Task low-late" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(urgent).toHaveFocus();
    await user.keyboard("{End}");
    expect(laneHead(/^Closed/)).toHaveFocus();
    await user.keyboard("{Home}");
    expect(triageHead).toHaveFocus();
  });

  it("the board shows the same five groups as columns and shares the selection", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "Board" }));
    const triage = screen.getByRole("region", { name: /^Triage/ });
    expect(within(triage).getByText("Task urgent")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: /^Waiting/ })).getByText("Task blocked")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /^Closed/ })).toBeInTheDocument();
    await user.click(within(triage).getByRole("button", { name: /Task urgent/ }));
    await screen.findByRole("complementary", { name: "Task detail" });
    await user.click(screen.getByRole("button", { name: "List" }));
    expect(screen.getByRole("row", { name: "Task urgent" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("complementary", { name: "Task detail" })).toBeInTheDocument();
  });

  it("a card dragged onto In progress starts it; the columns it cannot reach are dimmed", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "Board" }));
    const card = within(screen.getByRole("region", { name: /^To do/ })).getByRole("button", { name: /Task low-late/ });
    const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(card, { dataTransfer });
    const running = screen.getByRole("region", { name: /^In progress/ });
    await waitFor(() => expect(running).toHaveAttribute("data-drop", "ok"));
    expect(screen.getByRole("region", { name: /^Triage/ })).toHaveAttribute("data-drop", "no");
    fireEvent.dragOver(running, { dataTransfer });
    fireEvent.drop(running, { dataTransfer });
    await waitFor(() => expect(claimTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "low-late"));
    expect(running).not.toHaveAttribute("data-drop");
  });

  it("keys: s starts the selected to-do row", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.keyboard("jj");
    expect(screen.getByRole("row", { name: "Task low-late" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("s");
    await waitFor(() => expect(claimTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "low-late"));
  });

  it("keys act on a deep-linked task past a lane's first rows", async () => {
    const many = Array.from({ length: 25 }, (_, i) =>
      task(`t${String(i).padStart(2, "0")}`, { created_at: iso(100 - i) }),
    );
    vi.mocked(listTasks).mockResolvedValue({ tasks: many, truncated: false });
    window.history.replaceState(null, "", "/tasks?task=t24");
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("complementary", { name: "Task detail" });
    // t24 is not drawn: the To do lane shows its first 20.
    expect(screen.queryByRole("row", { name: "Task t24" })).toBeNull();
    await user.keyboard("s");
    await waitFor(() => expect(claimTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "t24"));
  });

  it("on the board, a key acts on the card that has focus, not the one selected before", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [task("t-a", { created_at: iso(3) }), task("t-b", { created_at: iso(2) })],
      truncated: false,
    });
    localStorage.setItem("loom_tasks_mode", "board");
    try {
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("region", { name: "Tasks board" });
      await user.click(document.querySelector<HTMLElement>('[data-task-id="t-a"]') as HTMLElement);
      await screen.findByRole("complementary", { name: "Task detail" });
      await user.keyboard("{Escape}");
      document.querySelector<HTMLElement>('[data-task-id="t-b"]')?.focus();
      await user.keyboard("s");
      await waitFor(() => expect(claimTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "t-b"));
      expect(claimTask).not.toHaveBeenCalledWith(expect.anything(), "t-a");
    } finally {
      localStorage.removeItem("loom_tasks_mode");
    }
  });

  it("on the board, j moves from the card that has focus", async () => {
    vi.mocked(listTasks).mockResolvedValue({
      tasks: [
        task("t-a", { created_at: iso(5) }),
        task("t-b", { created_at: iso(4) }),
        task("t-c", { created_at: iso(3) }),
      ],
      truncated: false,
    });
    localStorage.setItem("loom_tasks_mode", "board");
    try {
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("region", { name: "Tasks board" });
      await user.click(document.querySelector<HTMLElement>('[data-task-id="t-a"]') as HTMLElement);
      await screen.findByRole("complementary", { name: "Task detail" });
      await user.keyboard("{Escape}");
      document.querySelector<HTMLElement>('[data-task-id="t-b"]')?.focus();
      await user.keyboard("j");
      await waitFor(() =>
        expect(document.querySelector('[data-task-id="t-c"]')).toHaveAttribute("aria-current", "true"),
      );
    } finally {
      localStorage.removeItem("loom_tasks_mode");
    }
  });

  it("a selected row a filter hid, with no panel open, takes no keys", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.keyboard("jj");
    expect(screen.getByRole("row", { name: "Task low-late" })).toHaveAttribute("aria-selected", "true");
    await user.type(screen.getByLabelText("Search tasks"), "urgent");
    await waitFor(() => expect(screen.queryByRole("row", { name: "Task low-late" })).toBeNull());
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("s");
    expect(claimTask).not.toHaveBeenCalled();
  });

  it("a failed first task.counts leaves the rail's counts unknown, never 'empty'", async () => {
    vi.mocked(taskCounts).mockRejectedValueOnce(new Error("down"));
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await waitFor(() => expect(taskCounts).toHaveBeenCalled());
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(rail).queryByText("empty")).toBeNull();
  });

  it("reads counts a failed read left unknown again on the next poll", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(taskCounts).mockRejectedValueOnce(new Error("down"));
      seed(admin, [row("admin")]);
      render(<Tasks />);
      await screen.findByRole("row", { name: "Task urgent" });
      expect(taskCounts).toHaveBeenCalledTimes(1);
      await act(async () => {
        vi.advanceTimersByTime(60_000);
      });
      await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(2));
      const rail = screen.getByRole("navigation", { name: "Inboxes" });
      await waitFor(() => expect(within(rail).queryByText("…")).toBeNull());
    } finally {
      vi.useRealTimers();
    }
  });

  it("a failed poll after a good read keeps the tasks and says it could not refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      seed(admin, [row("admin")]);
      render(<Tasks />);
      await screen.findByRole("row", { name: "Task urgent" });
      vi.mocked(listTasks).mockRejectedValueOnce(new Error("down"));
      await act(async () => {
        vi.advanceTimersByTime(60_000);
      });
      await waitFor(() => expect(screen.getByText(/couldn't refresh the tasks/)).toBeInTheDocument());
      expect(screen.getByRole("row", { name: "Task urgent" })).toBeInTheDocument();
      expect(screen.queryByText("Couldn't load tasks")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("an empty board column under a filter says none match", async () => {
    localStorage.setItem("loom_tasks_mode", "board");
    try {
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("region", { name: "Tasks board" });
      expect(screen.queryByText("None match the filters.")).toBeNull();
      await user.type(screen.getByLabelText("Search tasks"), "urgent");
      await waitFor(() => expect(screen.getAllByText("None match the filters.").length).toBeGreaterThan(0));
      expect(screen.queryByText("Nothing closed yet.")).toBeNull();
    } finally {
      localStorage.removeItem("loom_tasks_mode");
    }
  });

  it("an empty Closed lane under a filter says none match, not that nothing closed", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.type(screen.getByLabelText("Search tasks"), "urgent");
    await waitFor(() => expect(screen.queryByRole("row", { name: "Task low-late" })).toBeNull());
    expect(screen.queryByText("Nothing closed yet.")).toBeNull();
    expect(screen.getAllByText("None match the filters.").length).toBeGreaterThan(0);
  });

  it("with no inbox registered, explains and offers Register inbox to an admin only", async () => {
    vi.mocked(listInboxes).mockResolvedValueOnce([]);
    seed(viewer, [row("viewer")]);
    render(<Tasks />);
    await screen.findByRole("heading", { name: "No inbox here yet" });
    expect(screen.queryByRole("button", { name: "Register inbox" })).not.toBeInTheDocument();
    expect(listTasks).not.toHaveBeenCalled();
    cleanup();
    vi.mocked(listInboxes).mockResolvedValueOnce([]);
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("heading", { name: "No inbox here yet" });
    expect(screen.getByRole("button", { name: "Register inbox" })).toBeInTheDocument();
  });

  it("picking inbox B while A's task.list is in flight drops A's answer", async () => {
    let answerA: (v: { tasks: TaskListRow[]; truncated: boolean }) => void = () => {};
    vi.mocked(listInboxes).mockResolvedValueOnce([INBOX, INBOX2]);
    vi.mocked(listTasks)
      .mockImplementationOnce(() => new Promise((r) => (answerA = r)))
      .mockResolvedValueOnce({ tasks: [task("from-b", { inbox: INBOX2.inbox })], truncated: false });
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(1));
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    await user.click(within(rail).getByRole("button", { name: /public-bot/ }));
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(2));
    expect(vi.mocked(listTasks).mock.calls[1][1]?.inbox).toBe("public-bot");
    await screen.findByRole("row", { name: "Task from-b" });
    answerA({ tasks: ROWS, truncated: false });
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("row", { name: "Task urgent" })).toBeNull();
    expect(screen.getByRole("row", { name: "Task from-b" })).toBeInTheDocument();
  });

  it("a namespace switch during inbox.list restores only the new namespace's inboxes", async () => {
    let answerA: (v: Inbox[]) => void = () => {};
    vi.mocked(listInboxes)
      .mockImplementationOnce(() => new Promise((r) => (answerA = r)))
      .mockResolvedValueOnce([INBOX2]);
    vi.mocked(listTasks).mockResolvedValue({ tasks: [task("from-b", { inbox: INBOX2.inbox })], truncated: false });
    seed(admin, [row("admin"), { ...row("admin"), namespace: "other" }]);
    render(<Tasks />);
    await waitFor(() => expect(listInboxes).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    await waitFor(() => expect(listInboxes).toHaveBeenCalledTimes(2));
    await screen.findByRole("row", { name: "Task from-b" });
    answerA([INBOX]);
    await new Promise((r) => setTimeout(r, 0));
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(rail).queryByRole("button", { name: /private-bot/ })).toBeNull();
    expect(new URLSearchParams(window.location.search).get("inbox")).toBe("public-bot");
    expect(vi.mocked(listTasks).mock.calls.every((c) => c[0].namespace === "other")).toBe(true);
  });

  it("a refresh after an action wins over a poll that was in flight before it", async () => {
    // Only the interval is faked: the 60 s poll fires on demand, waitFor keeps real timeouts.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      let answerPoll: (v: { tasks: TaskListRow[]; truncated: boolean }) => void = () => {};
      const accepted = ROWS.map((r) => (r.task_id === "urgent" ? { ...r, status: "todo" as const } : r));
      vi.mocked(listTasks)
        .mockResolvedValueOnce({ tasks: ROWS, truncated: false })
        .mockImplementationOnce(() => new Promise((r) => (answerPoll = r)))
        .mockResolvedValue({ tasks: accepted, truncated: false });
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("row", { name: "Task urgent" });
      act(() => {
        vi.advanceTimersByTime(60_000);
      });
      expect(listTasks).toHaveBeenCalledTimes(2);
      await user.click(screen.getByRole("button", { name: "Accept Task urgent" }));
      await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(3));
      await waitFor(() => expect(groupRows(/^To do/)).toEqual(["Task urgent", "Task low-late"]));
      answerPoll({ tasks: ROWS, truncated: false });
      await new Promise((r) => setTimeout(r, 0));
      expect(groupRows(/^To do/)).toEqual(["Task urgent", "Task low-late"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a stored mode outside list / board opens the list; a stored board opens the board", async () => {
    localStorage.setItem("loom_tasks_mode", "grid");
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(screen.getByRole("button", { name: "List" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Board" })).toHaveAttribute("aria-pressed", "false");
    cleanup();
    localStorage.setItem("loom_tasks_mode", "board");
    render(<Tasks />);
    await screen.findByRole("region", { name: /^Triage/ });
    expect(screen.getByRole("button", { name: "Board" })).toHaveAttribute("aria-pressed", "true");
  });

  it("a namespace switch while A's task.counts is in flight shows only B's counts", async () => {
    let answerA: (v: InboxCount[]) => void = () => {};
    vi.mocked(listInboxes).mockResolvedValueOnce([INBOX]).mockResolvedValueOnce([INBOX2]);
    vi.mocked(listTasks).mockResolvedValue({ tasks: [task("from-b", { inbox: INBOX2.inbox })], truncated: false });
    vi.mocked(taskCounts)
      .mockImplementationOnce(() => new Promise((r) => (answerA = r)))
      .mockResolvedValueOnce([{ inbox: "public-bot", status: "todo", blocked_on: "", n: 2, oldest: null }]);
    seed(admin, [row("admin"), { ...row("admin"), namespace: "other" }]);
    render(<Tasks />);
    await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(2));
    const rail = screen.getByRole("navigation", { name: "Inboxes" });
    await within(rail).findByRole("button", { name: /public-bot.*2 to do/ });
    await act(async () => {
      answerA([{ inbox: "public-bot", status: "todo", blocked_on: "", n: 9, oldest: null }]);
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(within(rail).getByRole("button", { name: /public-bot.*2 to do/ })).toBeInTheDocument();
    expect(within(rail).queryByRole("button", { name: /9 to do/ })).toBeNull();
  });

  it("an inbox with nothing in it shows the empty state, whose New task is the page's one primary", async () => {
    vi.mocked(listTasks).mockResolvedValueOnce({ tasks: [], truncated: false });
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("heading", { name: "No tasks in private-bot yet" });
    expect(screen.getByText("Agents post tasks here; people use New task.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /New task/ })).toHaveLength(1);
    // The toolbar stays, so nothing jumps when a task lands.
    expect(screen.getByLabelText("Search and filters")).toBeInTheDocument();
  });

  // On a phone under a touch pointer the four tiles, the kind and the poster fold into one
  // Filters menu beside the inbox, so the first task starts inside the chrome budget and no
  // tile label is cut.
  it("on a phone folds the tiles, the kind and the poster into one Filters menu", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: /max-width: (639|767|1279)px/.test(query),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    try {
      seed(admin, [row("admin")]);
      const user = userEvent.setup();
      render(<Tasks />);
      await screen.findByRole("row", { name: "Task urgent" });
      expect(screen.queryByRole("group", { name: "Needs you" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Posted by" })).toBeNull();
      await user.click(screen.getByRole("button", { name: "Filters" }));
      const menu = await screen.findByRole("menu", { name: "Filters" });
      expect(within(menu).getByRole("menuitemradio", { name: "Everything" })).toHaveAttribute("aria-checked", "true");
      await user.click(within(menu).getByRole("menuitemradio", { name: "Failed · reopen or close (1)" }));
      expect(taskRows()).toEqual(["Task failed"]);
      // The trigger says a filter is on.
      await user.click(screen.getByRole("button", { name: "Filters (1)" }));
      await user.click(await screen.findByRole("menuitemradio", { name: "This agent" }));
      expect(screen.getByRole("button", { name: "Filters (2)" })).toBeInTheDocument();
      expect(taskRows()).toEqual([]);
      await user.click(screen.getByRole("button", { name: "Clear filters" }));
      expect(screen.getByRole("button", { name: "Filters" })).toBeInTheDocument();
      // The open menu is checked by axe in e2e/tasks-ux.spec.ts.
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
