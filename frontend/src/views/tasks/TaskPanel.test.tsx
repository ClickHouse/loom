import * as RadixDialog from "@radix-ui/react-dialog";
import { useState } from "react";
import { act, cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Inbox, TaskEvent, TaskListRow, TaskRow, TaskStatus } from "../../api/tasks";
import { TaskPanel, actionTip } from "./TaskPanel";
import { must } from "../../test/must";

// The panel is a document, the footer is the verb. Sections come from the brief; the
// timeline reads newest first with heartbeats folded; each status offers its own actions,
// primary first.

const INBOX: Inbox = {
  org: "dev",
  inbox: "private-bot",
  owner_agent: "reporter",
  owner_namespace: "team",
  description: "",
  posters: [],
  accept_disclosure: ["internal"],
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
const NOW = Date.now();
const iso = (minAgo: number) => new Date(NOW - minAgo * 60_000).toISOString().replace(/\.\d+Z$/, "");

const listRow = (status: TaskStatus, extra: Partial<TaskListRow> = {}): TaskListRow => ({
  task_id: "0123456789abcdef",
  inbox: "private-bot",
  kind: "security_finding",
  priority: 1,
  status,
  reason: "",
  blocked_on: "",
  not_before: null,
  resume_at: null,
  deadline: null,
  defer_count: 0,
  title: "UB in JSONObjectReader on truncated input",
  from_agent: "reporter-public",
  from_subject: "sa:reporter-public-1a2b3c",
  owner: "",
  lease_until: null,
  parent_task_id: null,
  disclosure: "confidential",
  created_at: iso(42),
  updated_at: iso(2),
  version: 3,
  ...extra,
});
const fullRow = (status: TaskStatus, extra: Partial<TaskRow> = {}): TaskRow => ({
  ...listRow(status),
  org: "dev",
  namespace: "team",
  origin: "",
  attempt: 0,
  fence: 0,
  last_actor: "",
  brief: {
    objective: "Confirm and file in example-private; do not disclose publicly.",
    acceptance_criteria: ["Private issue filed with the reproducer", "Verdict recorded"],
    inputs: {
      pr: "ClickHouse/ClickHouse#120507",
      span: "src/Parsers/ASTJSONReadHelpers.cpp:87",
      test: "03011_json_read_expr_child_ub.sql",
      asan_log: "https://s3.example/asan-1.txt",
      expected: "exception 36",
    },
    constraints: "Confidential. The reproducer must not appear in any public artifact.",
  },
  outcome: {},
  ...extra,
});
const ev = (seq: number, event: string, actor: string, minAgo: number, extra: Partial<TaskEvent> = {}): TaskEvent => ({
  seq,
  task_id: "0123456789abcdef",
  inbox: "private-bot",
  event,
  from_status: "",
  to_status: "",
  actor,
  agent: "",
  fence: 0,
  version: seq,
  reason: "",
  patch: {},
  note: "",
  ts: iso(minAgo),
  ...extra,
});

vi.mock("../../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/tasks")>();
  return {
    ...actual,
    getTask: vi.fn(async () => fullRow("todo")),
    taskHistory: vi.fn(async () => [
      ev(1, "created", "process:reporter-public", 42),
      ev(2, "accepted", "human:alice@example.com", 30),
      ev(3, "claimed", "process:reporter", 20, { note: "lease 1,800 s" }),
      ev(4, "heartbeat", "process:reporter", 15),
      ev(5, "heartbeat", "process:reporter", 10),
      ev(6, "heartbeat", "process:reporter", 5),
      ev(7, "lease_expired", "process:task_lease_sweep", 1, { reason: "lease of 'reporter' expired" }),
    ]),
    listPolicies: vi.fn(async () => []),
    listTasks: vi.fn(async () => ({ tasks: [], truncated: false })),
  };
});
import { getTask, listPolicies, listTasks, taskHistory } from "../../api/tasks";

const identity = { org: "dev", namespace: "team" };
const noop = () => undefined;

function mount(status: TaskStatus, extra: Partial<TaskRow> = {}, props: Partial<Parameters<typeof TaskPanel>[0]> = {}) {
  vi.mocked(getTask).mockResolvedValueOnce(fullRow(status, extra));
  const onAction = vi.fn();
  render(
    <TaskPanel
      identity={identity}
      row={listRow(status, extra)}
      inboxes={[INBOX]}
      now={NOW}
      reloadKey={0}
      onClose={noop}
      onAction={onAction}
      onOpenTask={noop}
      writeReason={null}
      {...props}
    />,
  );
  return onAction;
}

describe("TaskPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(cleanup);

  it("renders the brief as sections with links and mono where the value asks for it, and the header names the handoff", async () => {
    mount("todo");
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    await within(panel).findByText("Confirm and file in example-private; do not disclose publicly.");
    expect(within(panel).getByRole("heading", { name: "Objective" })).toBeInTheDocument();
    expect(
      must(
        within(panel).getByRole("heading", { name: "Acceptance criteria" }).parentElement,
        "Acceptance criteria section",
      ).querySelectorAll("li"),
    ).toHaveLength(2);
    expect(
      within(panel).getByRole("link", { name: "ClickHouse/ClickHouse#120507 (opens in a new tab)" }),
    ).toHaveAttribute("href", "https://github.com/ClickHouse/ClickHouse/issues/120507");
    expect(within(panel).getByText("src/Parsers/ASTJSONReadHelpers.cpp:87")).toHaveClass("font-mono");
    expect(within(panel).getByText("03011_json_read_expr_child_ub.sql")).toHaveClass("font-mono");
    expect(within(panel).getByRole("link", { name: /s3.example\/asan-1.txt/ })).toHaveAttribute(
      "href",
      "https://s3.example/asan-1.txt",
    );
    expect(within(panel).getByText("exception 36")).not.toHaveClass("font-mono");
    expect(within(panel).getByText(/The reproducer must not appear/)).toHaveClass("tq-constraint");
    expect(within(panel).getByText("from reporter-public")).toBeInTheDocument();
    // The hand-off arrow is an icon; its text says "to".
    const handed = within(panel).getByText((_, el) => el?.tagName === "SPAN" && el.textContent === "to private-bot");
    expect(handed.querySelector("svg")).not.toBeNull();
    // The id is in the More menu (Copy id), not a truncated string in the header.
    expect(within(panel).queryByText("01234567…")).toBeNull();
    expect(
      within(panel)
        .getByText(/· posted/)
        .querySelector("time"),
    ).toHaveTextContent("42m ago");
    expect(getTask).toHaveBeenCalledTimes(1);
    expect(taskHistory).toHaveBeenCalledTimes(1);
    expect(listPolicies).not.toHaveBeenCalled(); // only a triage task reads the rule
    expect(listTasks).not.toHaveBeenCalled();
  });

  it("the timeline reads newest first, folds heartbeats, and tells a person, an agent and the sweeper apart", async () => {
    mount("todo");
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    await within(panel).findByText("lease expired");
    const items = must(
      within(panel).getByRole("heading", { name: "Timeline" }).parentElement,
      "Timeline section",
    ).querySelectorAll("li");
    const events = [...items].map((li) => must(li.querySelector("b"), "event label").textContent);
    expect(events).toEqual(["lease expired", "3 heartbeats", "claimed", "accepted", "posted"]);
    const kinds = [...items].map((li) => must(li.querySelector(".ic"), "event icon").getAttribute("data-kind"));
    expect(kinds).toEqual(["sweeper", "bot", "bot", "human", "bot"]);
    // Click UI icons, not letters standing in for them.
    for (const li of items) expect(must(li.querySelector(".ic"), "event icon").querySelector("svg")).not.toBeNull();
    expect(within(panel).getByText("lease of 'reporter' expired")).toBeInTheDocument();
  });

  it("a triage task shows the poster's record and the rule with its evidence check", async () => {
    vi.mocked(listPolicies).mockResolvedValueOnce([
      {
        org: "dev",
        inbox: "private-bot",
        poster: "sa:reporter-public-1a2b3c",
        kind: "*",
        mode: "review",
        require_evidence: ["inputs.pr", "repro"],
        set_by: "",
        set_at: "",
        version: 1,
      },
    ]);
    vi.mocked(listTasks).mockResolvedValueOnce({
      tasks: [listRow("done"), listRow("done"), listRow("wont_do"), listRow("triage")],
      truncated: false,
    });
    mount("triage");
    const panel = await screen.findByRole("complementary", { name: "Task detail" });
    const record = await within(panel).findByLabelText(
      "4 tasks from this poster: 2 done, 1 won't do, 0 failed, 1 open",
    );
    // Each count is the dashboard's one stat tile (ui/Stat, Click UI's BigStat).
    expect(record.querySelectorAll("[class*='big-stat__title']")).toHaveLength(4);
    await within(panel).findByText(/Rule:/);
    expect(within(panel).getByText("review")).toBeInTheDocument();
    expect(within(panel).getByText("inputs.pr")).toBeInTheDocument();
    expect(within(panel).getByText(/missing/)).toBeInTheDocument();
    expect(vi.mocked(listTasks).mock.calls[0][1]).toMatchObject({
      inbox: "private-bot",
      postedBy: "sa:reporter-public-1a2b3c",
      fields: ["status", "kind", "created_at"],
    });
  });

  const footerOf = async () =>
    within(await screen.findByRole("complementary", { name: "Task detail" })).getByLabelText("Actions");
  const buttons = (el: HTMLElement) =>
    within(el)
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") ?? b.textContent);

  // The action bar sits under the header, in view at any height: one primary, at most one
  // secondary, every other move under More.
  it("actions per status: triage accepts, todo starts or defers, in progress finishes or fails; the rest under More", async () => {
    const onAction = mount("triage");
    let f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Won't do…", "Accept"]);
    await userEvent.setup().click(within(f).getByRole("button", { name: "Accept" }));
    expect(onAction).toHaveBeenCalledWith("accept", expect.objectContaining({ task_id: "0123456789abcdef" }));
    cleanup();

    mount("todo");
    f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Defer…", "Start"]);
    let user = userEvent.setup();
    await user.click(within(f).getByRole("button", { name: "More actions" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Block…",
      "Won't do…",
      "Edit brief",
      "Reassign…",
      "Cancel task…",
      "Copy id",
    ]);
    cleanup();

    mount("in_progress", { owner: "reporter", lease_until: iso(-22) });
    f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Fail…", "Mark done…"]);
    const panel = screen.getByRole("complementary", { name: "Task detail" });
    expect(panel).toHaveTextContent("claimed by reporter · lease ends in 22m");
    user = userEvent.setup();
    await user.click(within(f).getByRole("button", { name: "More actions" }));
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Block…",
      "Defer…",
      "Won't do…",
      "Cancel task…",
      "Copy id",
    ]);
  });

  it("footer per status: blocked unblocks, deferred resumes now (task.resume) and says when the sweeper would, closed short of done reopens, done only copies", async () => {
    let onAction = mount("blocked", { blocked_on: "sender", reason: "need the exact server flags" });
    let f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Won't do…", "Unblock"]);
    expect(screen.getByRole("heading", { name: "Blocked on sender" })).toBeInTheDocument();
    // Why it waits comes first, then the brief; the actions sit above them both.
    await screen.findByText(/Confirm and file/);
    const order = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(order.slice(0, 2)).toEqual(["Blocked on sender", "Objective"]);
    expect(
      f.compareDocumentPosition(screen.getByRole("heading", { name: "Blocked on sender" })) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByText("need the exact server flags")).toBeInTheDocument();
    cleanup();

    onAction = mount("deferred", { reason: "binary for merge SHA not built yet", resume_at: iso(-120) });
    f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Won't do…", "Resume now"]);
    expect(screen.getByRole("complementary", { name: "Task detail" })).toHaveTextContent("resumes in 2h");
    expect(screen.getByRole("heading", { name: "Deferred · resumes in 2h" })).toBeInTheDocument();
    await userEvent.setup().click(within(f).getByRole("button", { name: "Resume now" }));
    expect(onAction).toHaveBeenCalledWith("resume", expect.anything());
    cleanup();

    mount("failed", { reason: "bisect: no good build" });
    f = await footerOf();
    expect(buttons(f)).toEqual(["More actions", "Reopen…"]);
    expect(screen.getByRole("heading", { name: "Reason" })).toBeInTheDocument();
    cleanup();

    mount("done", {
      outcome: { issue_url: "https://github.com/ClickHouse/example-private/issues/4121", verdict: "confirmed" },
    });
    f = await footerOf();
    expect(buttons(f)).toEqual(["More actions"]);
    await screen.findByRole("heading", { name: "Outcome" });
    expect(
      screen.getByRole("link", {
        name: "https://github.com/ClickHouse/example-private/issues/4121 (opens in a new tab)",
      }),
    ).toBeInTheDocument();
  });

  it("without write access the footer holds the moves, unavailable with the reason, and a click sends nothing", async () => {
    const reason = "Needs writer on team. An admin of it can grant that.";
    const onAction = mount("triage", {}, { writeReason: reason });
    const f = await footerOf();
    const accept = within(f).getByRole("button", { name: "Accept" });
    expect(accept).toHaveAttribute("aria-disabled", "true");
    expect(accept).toHaveAccessibleDescription(reason);
    await userEvent.setup().click(accept);
    expect(onAction).not.toHaveBeenCalled();
  });

  it("a task the read does not find shows only that, with no brief to wait on and no actions", async () => {
    vi.mocked(getTask).mockResolvedValueOnce(null);
    render(
      <TaskPanel
        identity={identity}
        row={listRow("triage")}
        inboxes={[INBOX]}
        now={NOW}
        reloadKey={0}
        writeReason={null}
        onClose={noop}
        onAction={noop}
        onOpenTask={noop}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("This task is not visible to you, or no longer exists.");
    expect(screen.queryByRole("heading", { name: "Objective" })).toBeNull();
    // Past the loader's 300 ms delay, so a brief loader would be showing by now.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 350));
    });
    expect(screen.queryByText("Loading the brief")).toBeNull();
    expect(screen.queryByRole("group", { name: "Actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("reads again when the page bumps reloadKey after an action", async () => {
    vi.mocked(getTask).mockResolvedValue(fullRow("todo"));
    const { rerender } = render(
      <TaskPanel
        identity={identity}
        row={listRow("todo")}
        inboxes={[INBOX]}
        now={NOW}
        reloadKey={0}
        writeReason={null}
        onClose={noop}
        onAction={noop}
        onOpenTask={noop}
      />,
    );
    await waitFor(() => expect(getTask).toHaveBeenCalledTimes(1));
    rerender(
      <TaskPanel
        identity={identity}
        row={listRow("todo")}
        inboxes={[INBOX]}
        now={NOW}
        reloadKey={1}
        writeReason={null}
        onClose={noop}
        onAction={noop}
        onOpenTask={noop}
      />,
    );
    await waitFor(() => expect(getTask).toHaveBeenCalledTimes(2));
    expect(taskHistory).toHaveBeenCalledTimes(2);
  });
  // Under 1280 px the page renders the panel as a modal sheet: a dialog named by the task's
  // title that takes the focus, closes on Escape and hands the focus back to the task's row.
  it("as a sheet is a dialog that takes the focus, closes on Escape and returns it to the row", async () => {
    // Radix's Portal hands its child a ref to track it; the panel must take it.
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(getTask).mockResolvedValueOnce(fullRow("todo"));
    const row = listRow("todo");
    function Page() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" data-task-id={row.task_id} onClick={() => setOpen(true)}>
            row
          </button>
          <RadixDialog.Root open={open} onOpenChange={setOpen}>
            <RadixDialog.Portal>
              {open && (
                <TaskPanel
                  identity={identity}
                  row={row}
                  inboxes={[INBOX]}
                  now={NOW}
                  reloadKey={0}
                  writeReason={null}
                  onClose={() => setOpen(false)}
                  onAction={noop}
                  onOpenTask={noop}
                  sheet
                />
              )}
            </RadixDialog.Portal>
          </RadixDialog.Root>
        </>
      );
    }
    const user = userEvent.setup();
    render(<Page />);
    await user.click(screen.getByRole("button", { name: "row" }));
    const dialog = await screen.findByRole("dialog", { name: row.title });
    expect(dialog).toHaveFocus();
    expect(screen.queryByRole("complementary", { name: "Task detail" })).toBeNull();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("button", { name: "row" })).toHaveFocus();
    expect(errors.mock.calls.flat().join(" ")).not.toMatch(/cannot be given refs/);
    errors.mockRestore();
  });
});

describe("actionTip", () => {
  it("names the key beside the panel, and keeps the explanation without the key in a sheet", () => {
    const accept = { action: "accept", label: "Accept", key: "a" } as const;
    const start = { action: "start", label: "Start", tip: "Claim it: to do → in progress", key: "s" } as const;
    const done = { action: "complete", label: "Mark done…", key: "c" } as const;
    expect(actionTip(accept, true)).toBe("Accept · A");
    expect(actionTip(done, true)).toBe("Mark done · C");
    expect(actionTip(start, true)).toBe("Claim it: to do → in progress · S");
    expect(actionTip(start, false)).toBe("Claim it: to do → in progress");
    expect(actionTip(accept, false)).toBe("");
    expect(actionTip({ action: "unblock", label: "Unblock" }, true)).toBe("");
  });
});
