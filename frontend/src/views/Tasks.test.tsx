import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import type { Inbox, InboxCount, TaskListRow, TaskRow } from "../api/tasks";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { axeViolations } from "../test/axe";
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

  it("the list is a valid grid: rowgroups of rows of gridcells, axe-clean with every group open", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("checkbox", { name: "Show closed" }));
    await user.click(within(screen.getByRole("rowgroup", { name: /^Closed/ })).getByRole("button", { name: /Closed/ }));
    const grid = screen.getByRole("grid", { name: "Tasks by what happens next" });
    expect(await axeViolations(grid)).toEqual([]);
  });

  it("groups rows by what happens next, in queue order, with Closed hidden until asked for", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(groupRows(/^Triage/)).toEqual(["Task urgent"]);
    expect(groupRows(/^To do/)).toEqual(["Task low-late"]);
    expect(groupRows(/^In progress/)).toEqual(["Task running"]);
    expect(groupRows(/^Waiting/)).toEqual(["Task blocked"]);
    expect(screen.queryByRole("rowgroup", { name: /^Closed/ })).not.toBeInTheDocument();
    expect(screen.getByText("decide: accept or won't do")).toBeInTheDocument();
    // The row's one line: the reason for a wait, the poster and age in triage, the lease in progress.
    expect(screen.getByText("blocked on human: needs a repro")).toBeInTheDocument();
    expect(screen.getByText("from reporter-public · 42 min ago")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /lease: 6 min left/ })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show closed" }));
    const closed = screen.getByRole("rowgroup", { name: /^Closed/ });
    // collapsed by default: the header shows, the rows do not
    expect(within(closed).getByRole("button", { name: /Closed/ })).toHaveAttribute("aria-expanded", "false");
    await user.click(within(closed).getByRole("button", { name: /Closed/ }));
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

  it("filters by kind and by who posted (the inbox's own agent vs others)", async () => {
    seed(admin, [row("admin")]);
    const user = userEvent.setup();
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    await user.click(screen.getByRole("button", { name: "coverage_gap" }));
    expect(taskRows()).toEqual(["Task low-late"]);
    await user.click(screen.getByRole("button", { name: "coverage_gap" }));
    await user.click(screen.getByRole("button", { name: "this agent" }));
    expect(taskRows()).toEqual(["Task low-late"]);
    await user.click(screen.getByRole("button", { name: "other agents" }));
    expect(taskRows()).toEqual(["Task urgent", "Task running", "Task blocked"]);
  });

  it("lists every inbox with its to-do and running counts, and All inboxes reads one task.list per inbox", async () => {
    vi.mocked(listInboxes).mockResolvedValueOnce([INBOX, INBOX2]);
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
    expect(within(rail).getByRole("button", { name: /Register inbox/ })).toBeInTheDocument();
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
  });

  it("Won't do… asks for a reason, calls task.wont_do and the row lands in Closed", async () => {
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
    await user.click(screen.getByRole("button", { name: "Won't do Task urgent" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "duplicate of private#3990");
    await user.click(within(dialog).getByRole("button", { name: "Won't do" }));
    await waitFor(() =>
      expect(wontDoTask).toHaveBeenCalledWith({ org: "dev", namespace: "team" }, "urgent", "duplicate of private#3990"),
    );
    await user.click(screen.getByRole("checkbox", { name: "Show closed" }));
    await user.click(within(screen.getByRole("rowgroup", { name: /^Closed/ })).getByRole("button", { name: /Closed/ }));
    await waitFor(() => expect(groupRows(/^Closed/)).toContain("Task urgent"));
    expect(screen.getByText("duplicate of private#3990")).toBeInTheDocument();
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
    expect(screen.queryByRole("button", { name: /Inbox settings/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Register inbox/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept Task urgent" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("row", { name: "Task urgent" }));
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    expect(within(panel).queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
  });

  it("an admin gets Inbox settings and New task", async () => {
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByRole("row", { name: "Task urgent" });
    expect(screen.getByRole("button", { name: /Inbox settings/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /New task/ })).toBeInTheDocument();
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
    await user.keyboard("{ArrowLeft}");
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
    expect(screen.getByRole("row", { name: "Task blocked" })).toHaveFocus();
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

  it("an inbox with nothing in it shows the empty state", async () => {
    vi.mocked(listTasks).mockResolvedValueOnce({ tasks: [], truncated: false });
    seed(admin, [row("admin")]);
    render(<Tasks />);
    await screen.findByText(/Nothing here yet/);
    expect(screen.getByText("task.create")).toBeInTheDocument();
  });
});
