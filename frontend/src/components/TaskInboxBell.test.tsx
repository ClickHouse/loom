import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { TaskInboxBell } from "./TaskInboxBell";

// The header bell asks `task.counts` once for the namespace (on idle after paint, then
// every two minutes while visible), counts triage
// + blocked, and opens the Tasks view on an inbox. A role the registry does
// not let read the counts sends nothing.

vi.mock("../api/tasks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/tasks")>();
  return {
    ...actual,
    taskCounts: vi.fn(async () => [
      { inbox: "private-bot", status: "triage", blocked_on: "", n: 2, oldest: "2026-09-20T01:00:00" },
      { inbox: "private-bot", status: "todo", blocked_on: "", n: 7, oldest: "2026-09-20T02:00:00" },
      { inbox: "pool", status: "blocked", blocked_on: "human", n: 1, oldest: "2026-09-19T00:00:00" },
      { inbox: "pool", status: "blocked", blocked_on: "sender", n: 5, oldest: "2026-09-18T00:00:00" },
    ]),
  };
});
import { taskCounts } from "../api/tasks";

const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team"],
};
const row = (role: MyNamespaceEntry["role"], kind: MyNamespaceEntry["kind"] = "memory"): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind,
});

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[] | null) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: "dev", namespace: "team", agent: "loom-ui" },
    view: "memories",
  });
}

async function settle() {
  for (let i = 0; i < 3; i++)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 30));
    });
}

describe("TaskInboxBell", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    sessionStorage.clear();
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, view: "memories" });
  });

  it("reads the counts once and badges triage + blocked on a person (not the five blocked on the sender)", async () => {
    seed(viewer, [row("viewer")]);
    render(<TaskInboxBell />);
    await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(1));
    expect(vi.mocked(taskCounts).mock.calls[0][0]).toEqual({ org: "dev", namespace: "team" });
    await screen.findByText("3");
    await settle();
    expect(taskCounts).toHaveBeenCalledTimes(1);
  });

  it("an unknown role, or a code namespace, sends nothing and renders no bell", async () => {
    seed(viewer, []);
    const { container } = render(<TaskInboxBell />);
    await settle();
    expect(taskCounts).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
    cleanup();
    seed(viewer, [row("admin", "code")]);
    render(<TaskInboxBell />);
    await settle();
    expect(taskCounts).not.toHaveBeenCalled();
  });

  it("the dropdown lists inboxes with something waiting and opens the Tasks view on one", async () => {
    seed(viewer, [row("viewer")]);
    const user = userEvent.setup();
    render(<TaskInboxBell />);
    await screen.findByText("3");
    await user.click(screen.getByRole("button", { name: "Tasks waiting for a person, 3" }));
    // The list loads on the first open.
    expect(await screen.findByText("private-bot")).toBeInTheDocument();
    expect(screen.getByText("2 in triage", { exact: false })).toBeInTheDocument();
    expect(
      screen.getByText("1 blocked on a person · 5 blocked on a sender or external event", { exact: false }),
    ).toBeInTheDocument();
    // The arrow is drawn, not spoken.
    expect(screen.getByRole("button", { name: /^private-bot.* Open$/ })).toBeInTheDocument();
    await user.click(screen.getByText("pool"));
    expect(useStore.getState().view).toBe("tasks");
    expect(new URLSearchParams(window.location.search).get("inbox")).toBe("pool");
  });

  it("opens an inbox as a new entry, leaving the page it left as it was", async () => {
    seed(viewer, [row("viewer")]);
    window.history.replaceState({ loomInspect: { key: "a/b", agent: "*" } }, "", "/memories?memory=a%2Fb");
    const user = userEvent.setup();
    render(<TaskInboxBell />);
    await screen.findByText("3");
    await user.click(screen.getByRole("button", { name: "Tasks waiting for a person, 3" }));
    await user.click(await screen.findByText("pool"));
    expect(window.location.pathname).toBe("/tasks");
    expect(window.location.search).toBe("?namespace=team&inbox=pool");
    // Back: the page left keeps its URL and the state the app keeps in it.
    await act(async () => {
      window.history.back();
      await new Promise((r) => window.addEventListener("popstate", r, { once: true }));
    });
    expect(window.location.pathname + window.location.search).toBe("/memories?memory=a%2Fb");
    expect((window.history.state as Record<string, unknown>).loomInspect).toEqual({ key: "a/b", agent: "*" });
  });

  it("a count read for the namespace it moved off never badges the new one", async () => {
    // team has 5 in triage, other has none; team's task.counts resolves after the switch.
    const triage = [{ inbox: "pool", status: "triage", blocked_on: "", n: 5, oldest: "2026-09-20T01:00:00" }];
    let resolveTeam: (rows: typeof triage) => void = () => {};
    vi.mocked(taskCounts)
      .mockImplementationOnce(
        () =>
          new Promise((r) => {
            resolveTeam = r;
          }),
      )
      .mockImplementationOnce(async () => []);
    seed(viewer, [row("viewer"), { ...row("viewer"), namespace: "other" }]);
    render(<TaskInboxBell />);
    await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(1));
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    await waitFor(() => expect(taskCounts).toHaveBeenCalledTimes(2));
    expect(vi.mocked(taskCounts).mock.calls[1][0]).toEqual({ org: "dev", namespace: "other" });
    await act(async () => resolveTeam(triage));
    await settle();
    expect(screen.queryByText("5")).not.toBeInTheDocument();
    // team's answer is still remembered for team.
    expect(sessionStorage.getItem("loom_badge_v1:taskbell:dev/team")).toContain('"n":5');
  });
});
