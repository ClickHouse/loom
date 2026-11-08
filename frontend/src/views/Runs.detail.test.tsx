import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunGetResponse, RunRow } from "../api";
import { useStore } from "../lib/state";
import { must } from "../test/must";
import { EMPTY_WHY, RUNS_FIELDS, Runs, hasNoMemoryActivity } from "./Runs";

// A run with no memory activity is folded whether loom inferred it or a program declared
// it; every empty field on the replay says who fills it; the list filters by agent;
// recalled keys open the memory and the replay links to Recall history for the run.

const listRuns = vi.fn<typeof import("../api").listRuns>();
const getRun = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    // The page reads run.list a page at a time; these tests answer with the rows alone.
    listRunsPage: async (...args: Parameters<typeof actual.listRunsPage>) => {
      const runs = await listRuns(...args);
      return { runs, total: runs.length, paged: true };
    },
    getRun: (...args: unknown[]) => getRun(...args),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

// A replay figure: the value under its label, and what its (i) says.
async function stat(dialog: HTMLElement, label: string): Promise<HTMLElement> {
  const term = await within(dialog).findByText(label, { selector: "dt" });
  return must(term.nextElementSibling as HTMLElement | null, `${label} value`);
}
async function helpOf(dialog: HTMLElement, label: string): Promise<string> {
  const about = within(dialog).getByRole("button", { name: `About ${label}` });
  act(() => about.focus());
  const text = (await screen.findByRole("tooltip")).textContent ?? "";
  act(() => about.blur());
  await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
  return text;
}

const row = (over: Partial<RunRow> = {}): RunRow => ({
  run_id: "r-hollow",
  agent: "reporter",
  user_id: "-",
  session_id: "-",
  goal: "ISSUE_TRIAGE issue_114194",
  user_request: "",
  expected_outcome: "",
  environment: "",
  model: "",
  consumer: "",
  status: "completed",
  started_at: "2026-09-21T17:00:00Z",
  completed_at: "2026-09-21T17:00:00Z",
  duration_ms: 17,
  final_response: "",
  confidence: 0,
  memories_used: [],
  memories_written: [],
  metadata: { work_type: "ISSUE_TRIAGE" },
  budget_usd: 0,
  budget_seconds: 0,
  grade: "",
  grade_source: "",
  grade_reason: "",
  graded_at: "",
  ...over,
});
const full = row({
  run_id: "r-full",
  agent: "claude-code",
  session_id: "s-1",
  memories_used: ["fact_a"],
  memories_written: ["note_b"],
  duration_ms: 90_000,
  goal: "fix the thing",
});

const detail = (over: Partial<RunGetResponse> = {}): RunGetResponse => ({
  found: true,
  run: null,
  recalled: 0,
  recalled_keys: [],
  memories_written: [],
  observations: 0,
  turns: 0,
  cost_usd: 0,
  unpriced_turns: 0,
  elapsed_seconds: 0,
  over_budget: [],
  ...over,
});

describe("hasNoMemoryActivity", () => {
  it("folds an explicit hollow run and an implicit one alike; a run with any memory activity is not folded", () => {
    expect(hasNoMemoryActivity(row())).toBe(true);
    expect(hasNoMemoryActivity(row({ metadata: { implicit: true } }))).toBe(true);
    expect(hasNoMemoryActivity(row({ memories_used: ["k"] }))).toBe(false);
    expect(hasNoMemoryActivity(row({ memories_written: ["k"] }))).toBe(false);
  });
});

describe("Runs page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getRun.mockResolvedValue(detail());
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "runs",
      recallRunFilter: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null, recallRunFilter: null });
  });

  it("asks run.list to fold runs with no memory activity, and refetches everything on the toggle", async () => {
    // The fold is the server's WHERE, so the page never sees the hollow rows until the
    // reader asks — the newest page of a chatty consumer can all be hollow, and slicing
    // after LIMIT would leave the page empty.
    listRuns.mockImplementation(async (_id, opts) => (opts?.memoryActivity === "with" ? [full] : [row(), full]));
    render(<Runs />);
    expect(await screen.findByText("fix the thing")).toBeInTheDocument();
    expect(screen.queryByText("ISSUE_TRIAGE issue_114194")).toBeNull();
    const [, first] = listRuns.mock.calls[0];
    expect(first?.memoryActivity).toBe("with");
    expect(first?.limit).toBe(50);
    fireEvent.click(screen.getByText("Show runs with no memory activity"));
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2));
    const [, second] = listRuns.mock.calls[1];
    expect(second?.memoryActivity).toBe("");
    expect(await screen.findByText("ISSUE_TRIAGE issue_114194")).toBeInTheDocument();
    expect(screen.getByText("no memory activity")).toBeInTheDocument();
    // The label stays put; the checked state says the folded runs are shown.
    const toggle = screen.getByRole("checkbox", { name: "Show runs with no memory activity" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("Hide runs with no memory activity")).toBeNull();
  });

  it("says so when the folded page is empty, without claiming there are no runs", async () => {
    listRuns.mockImplementation(async (_id, opts) => (opts?.memoryActivity === "with" ? [] : [row()]));
    render(<Runs />);
    expect(await screen.findByText("No runs with memory activity")).toBeInTheDocument();
    expect(screen.queryByText("No runs yet")).toBeNull();
    fireEvent.click(screen.getByText("Show runs with no memory activity"));
    expect(await screen.findByText("ISSUE_TRIAGE issue_114194")).toBeInTheDocument();
  });

  it("filters by agent through run.list", async () => {
    listRuns.mockResolvedValue([row(), full]);
    render(<Runs />);
    await screen.findByText("fix the thing");
    await userEvent.click(await screen.findByRole("button", { name: "Agent" }));
    // Click UI's Select items carry no option role; they are addressed by value.
    await userEvent.click(
      must(document.querySelector<HTMLElement>('[cui-select-item][data-value="reporter"]'), "reporter item"),
    );
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2));
    const [, opts] = listRuns.mock.calls[1];
    expect(opts?.agent).toBe("reporter");
  });

  it("another namespace reads its runs for every agent, not the agent picked in the last", async () => {
    listRuns.mockResolvedValue([row(), full]);
    render(<Runs />);
    await screen.findByText("fix the thing");
    await userEvent.click(await screen.findByRole("button", { name: "Agent" }));
    await userEvent.click(
      must(document.querySelector<HTMLElement>('[cui-select-item][data-value="reporter"]'), "reporter item"),
    );
    await waitFor(() => expect(listRuns.mock.calls.at(-1)?.[1]?.agent).toBe("reporter"));
    const before = useStore.getState().identity!;
    act(() => useStore.setState({ identity: { ...before, namespace: `${before.namespace}-2` } }));
    await waitFor(() => expect(listRuns.mock.calls.at(-1)?.[0]).toMatchObject({ namespace: `${before.namespace}-2` }));
    expect(listRuns.mock.calls.at(-1)?.[1]?.agent).toBe("");
  });

  it("the replay of a hollow run says what each empty field would have needed", async () => {
    listRuns.mockResolvedValue([row()]);
    render(<Runs />);
    fireEvent.click(await screen.findByText("Show runs with no memory activity"));
    fireEvent.click(await screen.findByRole("button", { name: "ISSUE_TRIAGE issue_114194" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(1));
    expect(within(dialog).getByText(/reported no memory activity/)).toBeInTheDocument();
    expect(within(dialog).getByText(EMPTY_WHY.used)).toBeInTheDocument();
    expect(within(dialog).getByText(EMPTY_WHY.written)).toBeInTheDocument();
    expect(await stat(dialog, "Recalled")).toHaveTextContent("0");
    expect(await helpOf(dialog, "Recalled")).toContain(EMPTY_WHY.recalledZero);
    // No top-level session: turns and cost cannot be joined, and the page says so.
    expect(within(dialog).getAllByText("no session")).toHaveLength(2);
    expect(await helpOf(dialog, "Turns")).toContain(EMPTY_WHY.noSession);
    expect(await helpOf(dialog, "Cost")).toContain(EMPTY_WHY.noSession);
    expect(within(dialog).queryByText(/inferred from a connection/)).toBeNull();
  });

  it("a recalled key opens the memory and the replay links to Recall history for the run", async () => {
    listRuns.mockResolvedValue([full]);
    getRun.mockResolvedValue(detail({ recalled: 2, recalled_keys: ["fact_a", "fact_c"], turns: 3, cost_usd: 0.0123 }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("fact_c")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "fact_c" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("fact_c");
    expect(within(dialog).getByText("$0.0123")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /Recall history for this run/ }));
    expect(useStore.getState().view).toBe("recall");
    expect(useStore.getState().recallRunFilter).toBe("r-full");
  });

  it("shows the verdict run.get carries, which the list row does not", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "dev", roles: ["admin"] } });
    // run.list answers with RUNS_FIELDS alone, so the row has no grade fields.
    const listed = Object.fromEntries(RUNS_FIELDS.map((f) => [f, full[f as keyof RunRow]])) as unknown as RunRow;
    listRuns.mockResolvedValue([listed]);
    getRun.mockResolvedValue(detail({ run: { ...full, grade: "fail", grade_source: "ci", grade_reason: "red" } }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Graded as did not hold by ci: red")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Regrade: held up" })).toBeInTheDocument();
  });

  it("shows the run's budget and which part of it the run went past", async () => {
    const budgeted = { ...full, budget_usd: 1, budget_seconds: 1800 };
    listRuns.mockResolvedValue([budgeted]);
    getRun.mockResolvedValue(detail({ run: budgeted, cost_usd: 1.25, elapsed_seconds: 42, over_budget: ["cost"] }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    const budget = await stat(dialog, "Budget");
    expect(budget).toHaveTextContent("$1.00 · 30m 0s · 42s elapsed · over cost");
  });

  it("shows a spend just past its budget to the place it went over", async () => {
    const budgeted = { ...full, budget_usd: 1 };
    listRuns.mockResolvedValue([budgeted]);
    getRun.mockResolvedValue(detail({ run: budgeted, cost_usd: 1.000004, over_budget: ["cost"] }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("$1.000004")).toBeInTheDocument();
    expect(await stat(dialog, "Budget")).toHaveTextContent("$1.00 · over cost");
  });

  it("spells a large spend with thousands separators, as the hint does", async () => {
    listRuns.mockResolvedValue([full]);
    getRun.mockResolvedValue(detail({ cost_usd: 12345 }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("$12,345.00")).toBeInTheDocument();
  });

  it("a run with a time budget that has just begun shows 0s elapsed", async () => {
    const budgeted = { ...full, budget_seconds: 60 };
    listRuns.mockResolvedValue([budgeted]);
    getRun.mockResolvedValue(detail({ run: budgeted, elapsed_seconds: 0 }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await stat(dialog, "Budget")).toHaveTextContent("60s · 0s elapsed");
  });

  it("names the turns it could not price", async () => {
    listRuns.mockResolvedValue([full]);
    getRun.mockResolvedValue(detail({ turns: 3, unpriced_turns: 2 }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await stat(dialog, "Turns")).toHaveTextContent("(2 unpriced)");
    expect(await helpOf(dialog, "Turns")).toContain(EMPTY_WHY.unpriced);
  });

  it("a run without a budget shows a dash", async () => {
    listRuns.mockResolvedValue([full]);
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await stat(dialog, "Budget")).toHaveTextContent("—");
  });

  it("drops a run.list answer that lands after the status filter changed", async () => {
    let answerAll: (v: RunRow[]) => void = () => {};
    listRuns
      .mockImplementationOnce(() => new Promise((r) => (answerAll = r)))
      .mockResolvedValueOnce([
        row({ run_id: "r-failed", status: "failed", goal: "the failed one", memories_used: ["k"] }),
      ]);
    render(<Runs />);
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(1));
    const statuses = screen.getByRole("group", { name: "Filter runs by status" });
    expect(within(statuses).getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(statuses).getByRole("button", { name: "Failed" }));
    expect(within(statuses).getByRole("button", { name: "Failed" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2));
    expect(listRuns.mock.calls[1][1]?.status).toBe("failed");
    expect(await screen.findByText("the failed one")).toBeInTheDocument();
    answerAll([full]);
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("fix the thing")).toBeNull();
    expect(screen.getByText("the failed one")).toBeInTheDocument();
  });

  it("the drawer for run Y never shows run X's replay when X's run.get lands last", async () => {
    const other = row({ run_id: "r-other", goal: "the other run", memories_used: ["k"] });
    listRuns.mockResolvedValue([full, other]);
    let answerX: (v: RunGetResponse) => void = () => {};
    getRun
      .mockImplementationOnce(() => new Promise((r) => (answerX = r)))
      .mockResolvedValueOnce(detail({ recalled: 1, recalled_keys: ["key_from_y"] }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "the other run" }));
    dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("key_from_y")).toBeInTheDocument();
    answerX(detail({ recalled: 1, recalled_keys: ["key_from_x"] }));
    await new Promise((r) => setTimeout(r, 0));
    expect(within(dialog).queryByText("key_from_x")).toBeNull();
    expect(within(dialog).getByText("key_from_y")).toBeInTheDocument();
  });

  it("recalled n/a when the trace log is off, with no link to a history that cannot exist", async () => {
    listRuns.mockResolvedValue([full]);
    getRun.mockResolvedValue(detail({ recalled: -1 }));
    render(<Runs />);
    fireEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
    const dialog = await screen.findByRole("dialog");
    expect(await stat(dialog, "Recalled")).toHaveTextContent("n/a");
    expect(await helpOf(dialog, "Recalled")).toContain(EMPTY_WHY.recalledOff);
    expect(within(dialog).queryByRole("button", { name: /Recall history for this run/ })).toBeNull();
  });
});
