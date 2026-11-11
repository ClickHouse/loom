import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunListOptions, RunListPage, RunRow } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// The search box keeps what is typed: a keystroke renders the box, not the table. Each row
// renders its started time, so ListTime counts the rows rendered.

const renders = { count: 0 };
vi.mock("../components/ui/ListTime", () => ({
  ListTime: ({ iso }: { iso: string }) => {
    renders.count += 1;
    return <time>{iso}</time>;
  },
}));

const listRunsPage = vi.fn<(id: unknown, opts?: RunListOptions) => Promise<RunListPage>>();
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listRunsPage: (id: unknown, opts?: RunListOptions) => listRunsPage(id, opts),
  listMine: vi.fn(async () => []),
  isAuthDisabled: () => false,
}));

const run = (i: number): RunRow => ({
  run_id: `run-${i}`,
  agent: "claude-code",
  user_id: "-",
  session_id: "s-1",
  goal: `Triage lane ${i}`,
  user_request: "",
  expected_outcome: "",
  environment: "",
  model: "",
  consumer: "",
  status: "completed",
  started_at: "2026-09-20T08:00:00Z",
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
});

describe("Runs row renders", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    renders.count = 0;
    listRunsPage.mockResolvedValue({ runs: [run(1), run(2), run(3)], total: 3, paged: true });
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "ns", agent: "loom-ui" }, view: "runs" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("a keystroke in the search renders no row again", async () => {
    render(<Runs />);
    await screen.findByText("Triage lane 3");
    renders.count = 0;
    act(() => {
      fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "lane" } });
    });
    expect(renders.count).toBe(0);
  });

  it("with no run says what a run is and offers Connect an agent", async () => {
    listRunsPage.mockResolvedValue({ runs: [], total: 0, paged: true });
    render(<Runs />);
    // Runs with no memory activity are folded at first: that state's action unfolds them, and
    // Connect an agent sits beside it as the secondary.
    expect(await screen.findByRole("heading", { name: "No runs with memory activity" })).toBeInTheDocument();
    expect(screen.queryByText(/run_id|memory\.set|run\.complete/)).toBeNull();
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show runs with no memory activity" }));
    expect(await screen.findByRole("heading", { name: "No runs yet" })).toBeInTheDocument();
    expect(
      screen.getByText(/A run is one session of an agent's work: what it read and what it wrote/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/run\.begin/)).toBeNull();
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    // The unfold left with its state: focus is on the toolbar's toggle, now checked.
    const toggle = screen.getByRole("checkbox", { name: "Show runs with no memory activity" });
    expect(toggle).toHaveFocus();
    expect(toggle).toBeChecked();
  });
});
