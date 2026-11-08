import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunListOptions, RunListPage, RunRow } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// Under 769 px Click UI's Table stacks seven label/value pairs per run (~240 px each);
// there the runs are list rows of two lines: the goal and its status, then agent ·
// started · took. Grouped by session, each session is a heading over its runs.

const listRunsPage = vi.fn<(id: unknown, opts?: RunListOptions) => Promise<RunListPage>>();
const getRun = vi.fn(async () => ({
  found: false,
  run: null,
  recalled: -1,
  recalled_keys: [],
  memories_written: [],
  over_budget: [],
  unpriced_turns: 0,
  elapsed_seconds: 0,
}));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listRunsPage: (id: unknown, opts?: RunListOptions) => listRunsPage(id, opts),
    getRun: () => getRun(),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const run = (run_id: string, goal: string, session_id: string, over: Partial<RunRow> = {}): RunRow => ({
  run_id,
  agent: "claude-code",
  user_id: "-",
  session_id,
  goal,
  user_request: "",
  expected_outcome: "",
  environment: "",
  model: "",
  consumer: "",
  status: "completed",
  started_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
  completed_at: "",
  duration_ms: 42_000,
  final_response: "",
  confidence: 1,
  memories_used: ["a"],
  memories_written: [],
  metadata: {},
  budget_usd: 0,
  budget_seconds: 0,
  grade: "",
  grade_source: "",
  grade_reason: "",
  graded_at: "",
  ...over,
});

function phoneWidth() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query === "(max-width: 639px)" || query === "(max-width: 768px)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

describe("Runs rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "runs",
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ identity: null });
  });

  it("are two-line list rows on a phone, not a table", async () => {
    phoneWidth();
    listRunsPage.mockResolvedValue({
      runs: [run("r-1", "fix the thing", "s-1", { status: "failed" }), run("r-2", "ship it", "s-1")],
      total: 2,
      paged: true,
    });
    render(<Runs />);
    const open = await screen.findByRole("button", { name: "fix the thing" });
    expect(screen.queryByRole("table")).toBeNull();
    const row = open.closest(".list-row") as HTMLElement;
    expect(row).not.toBeNull();
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(row).toHaveTextContent("claude-code");
    expect(row).toHaveTextContent("3h ago");
    expect(row).toHaveTextContent("42s");
    // One tab stop for the list.
    const opens = document.querySelectorAll<HTMLElement>("[data-row-open]");
    expect([...opens].map((el) => el.tabIndex)).toEqual([0, -1]);
    fireEvent.click(open);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("offers the sort as a select under 769 px, where there are no column headers", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === "(max-width: 768px)",
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    listRunsPage.mockResolvedValue({ runs: [run("r-1", "fix the thing", "s-1")], total: 1, paged: true });
    render(<Runs />);
    await screen.findByRole("button", { name: "fix the thing" });
    await userEvent.click(screen.getByRole("button", { name: "Sort runs" }));
    // Click UI's Select items carry no option role; they are addressed by value.
    const item = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[cui-select-item][data-value="duration_desc"]');
      expect(el).toHaveTextContent("Longest first");
      return el as HTMLElement;
    });
    await userEvent.click(item);
    await waitFor(() => expect(listRunsPage.mock.calls.at(-1)?.[1]).toMatchObject({ orderBy: "duration_desc" }));
  });

  // On a phone under a touch pointer the two toggles and the sort fold into one View menu
  // beside the filters, so the first run starts inside the chrome budget.
  it("folds the sort, the grouping and the hidden runs into one View menu on a phone", async () => {
    phoneWidth();
    listRunsPage.mockResolvedValue({ runs: [run("r-1", "fix the thing", "s-1")], total: 1, paged: true });
    render(<Runs />);
    await screen.findByRole("button", { name: "fix the thing" });
    expect(screen.queryByRole("checkbox", { name: "Group by session" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sort runs" })).toBeNull();
    const view = screen.getByRole("button", { name: "View" });
    await userEvent.click(view);
    const menu = await screen.findByRole("menu", { name: "View" });
    expect(within(menu).getByRole("menuitemradio", { name: "Newest first" })).toHaveAttribute("aria-checked", "true");
    expect(within(menu).getByRole("menuitemcheckbox", { name: "Group by session" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    await userEvent.click(within(menu).getByRole("menuitemradio", { name: "Longest first" }));
    await waitFor(() => expect(listRunsPage.mock.calls.at(-1)?.[1]).toMatchObject({ orderBy: "duration_desc" }));
    await userEvent.click(screen.getByRole("button", { name: "View" }));
    await userEvent.click(await screen.findByRole("menuitemcheckbox", { name: "Show runs with no memory activity" }));
    await waitFor(() => expect(listRunsPage.mock.calls.at(-1)?.[1]).toMatchObject({ memoryActivity: "" }));
    await userEvent.click(screen.getByRole("button", { name: "View" }));
    await userEvent.click(await screen.findByRole("menuitemcheckbox", { name: "Group by session" }));
    expect(await screen.findByRole("heading", { level: 2, name: "s-1" })).toBeInTheDocument();
  });

  it("put each session under its own heading when grouped", async () => {
    listRunsPage.mockResolvedValue({
      runs: [run("r-1", "first", "s-1"), run("r-2", "second", "s-2"), run("r-3", "third", "s-1")],
      total: 3,
      paged: true,
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "first" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Group by session" }));
    const headings = await screen.findAllByRole("heading", { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(["s-1", "s-2"]);
    expect(screen.getByText(/^2 runs · 2 relied on/)).toBeInTheDocument();
  });
});
