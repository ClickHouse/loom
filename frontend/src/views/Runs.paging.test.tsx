import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunRow } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// run.list has no offset: the page reads the newest 200 runs once and pages them here, 50
// at a time, in Click UI's Table. A full read says it is only the newest 200; the search
// and a filter start again from the first page.

const listRuns = vi.fn<typeof import("../api").listRuns>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listRuns: (...args: Parameters<typeof actual.listRuns>) => listRuns(...args),
    getRun: vi.fn(async () => ({ found: false, run: null })),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const run = (i: number): RunRow => ({
  run_id: `r-${i}`,
  agent: "claude-code",
  user_id: "-",
  session_id: "s-1",
  goal: `goal ${i}`,
  user_request: "",
  expected_outcome: "",
  environment: "",
  model: "",
  consumer: "",
  status: "completed",
  started_at: "2026-09-20T07:00:00Z",
  completed_at: "2026-09-20T07:05:00Z",
  duration_ms: 1000,
  final_response: "",
  confidence: 1,
  memories_used: ["a"],
  memories_written: [],
  metadata: {},
});
const runs = (n: number) => Array.from({ length: n }, (_, i) => run(i + 1));

const bodyRows = () =>
  within(screen.getByRole("table", { name: "Runs" }))
    .getAllByRole("row")
    .slice(1);

describe("Runs paging", () => {
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
    useStore.setState({ identity: null });
  });

  it("asks for the newest 200 and shows them 50 to a page", async () => {
    listRuns.mockResolvedValue(runs(120));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(listRuns.mock.calls[0][1]?.limit).toBe(200);
    expect(bodyRows()).toHaveLength(50);
    // jsdom gives `<search>` no role yet.
    expect(document.querySelector('search[aria-label="Search and filters"]')).toHaveTextContent("1–50 of 120");
    expect(screen.getByRole("navigation", { name: "Runs pages" })).toHaveTextContent("1–50 of 120");
    expect(screen.queryByText(/Newest 200 runs/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByRole("button", { name: "goal 51" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "goal 1" })).toBeNull();
    expect(bodyRows()).toHaveLength(50);
    expect(document.getElementById("runs-list")).toHaveFocus();
  });

  it("says a full read is only the newest 200 runs", async () => {
    listRuns.mockResolvedValue(runs(200));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(screen.getByText(/Newest 200 runs/)).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Runs pages" })).toHaveTextContent("1–50 of 200");
  });

  it("searches the goals it holds and starts again from the first page", async () => {
    listRuns.mockResolvedValue(runs(120));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    fireEvent.change(screen.getByRole("textbox", { name: "Search runs" }), { target: { value: "goal 11" } });
    // goal 11 and goal 110 to goal 119.
    await waitFor(() => expect(bodyRows()).toHaveLength(11));
    expect(screen.getByRole("button", { name: "goal 11" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Runs pages" })).toBeNull();
  });

  it("draws the first read as Click UI's loading table, marked busy", async () => {
    listRuns.mockReturnValue(new Promise(() => {}));
    render(<Runs />);
    const table = await screen.findByRole("table", { name: "Runs" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(table).getByText("Loading data")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Goal" })).toBeInTheDocument();
  });

  it("keeps the description to one sentence and the rest in its help", async () => {
    listRuns.mockResolvedValue(runs(1));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(screen.getByRole("button", { name: "About runs" })).toBeInTheDocument();
    expect(screen.queryByText(/group by it to see a whole working sitting/)).toBeNull();
  });
});
