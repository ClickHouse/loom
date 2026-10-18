import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunRow } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// A run's goal opens a tooltip only when the row cuts it short, and is read once; grouped
// by session, only the first table draws its column header, and every table keeps one
// for a screen reader.

const listRuns = vi.fn<typeof import("../api").listRuns>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    // The page reads run.list a page at a time; these tests answer with the rows alone.
    listRunsPage: async (...args: Parameters<typeof actual.listRunsPage>) => {
      const runs = await listRuns(...args);
      return { runs, total: runs.length, paged: true };
    },
    getRun: vi.fn(async () => ({ found: false, run: null })),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const run = (run_id: string, goal: string, session_id: string): RunRow => ({
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
  started_at: "2026-09-20T07:00:00Z",
  completed_at: "2026-09-20T07:05:00Z",
  duration_ms: 300_000,
  final_response: "",
  confidence: 1,
  memories_used: ["a"],
  memories_written: [],
  metadata: {},
  budget_usd: 0,
  budget_seconds: 0,
});

const LONG = "migrate every namespace's vector tables to the new width and re-embed what is stored";

// jsdom lays nothing out: the long goal's button reports a box narrower than its text.
function cutLongGoal() {
  const width = (el: HTMLElement, cut: number) => (el.textContent === LONG ? cut : 0);
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
    return width(this, 600);
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return width(this, 200);
  });
}

async function focusFor(el: HTMLElement) {
  act(() => el.focus());
  // Past the tooltip's open delay.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 400));
  });
}

describe("Runs table", () => {
  beforeEach(() => {
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "runs",
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useStore.setState({ identity: null });
  });

  it("opens no tooltip on a goal shown in full", async () => {
    listRuns.mockResolvedValue([run("r-1", "fix the thing", "s-1")]);
    render(<Runs />);
    const goal = await screen.findByRole("button", { name: "fix the thing" });
    await focusFor(goal);
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(goal).not.toHaveAttribute("aria-describedby");
  });

  it("shows a cut goal in a tooltip, without reading it twice", async () => {
    cutLongGoal();
    listRuns.mockResolvedValue([run("r-1", LONG, "s-1")]);
    render(<Runs />);
    const goal = await screen.findByRole("button", { name: LONG });
    await focusFor(goal);
    expect(await screen.findByRole("tooltip")).toHaveTextContent(LONG);
    expect(goal).toHaveAccessibleDescription("");
  });

  it("draws the column header over the first session only, and keeps it for the rest", async () => {
    listRuns.mockResolvedValue([run("r-1", "first", "s-1"), run("r-2", "second", "s-2"), run("r-3", "third", "s-3")]);
    render(<Runs />);
    await screen.findByRole("button", { name: "first" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Group by session" }));
    await waitFor(() => expect(screen.getAllByRole("table")).toHaveLength(3));
    const heads = screen.getAllByRole("table").map((t) => t.querySelector("thead"));
    expect(heads.map((h) => h?.matches(".table-header-sr-only > thead"))).toEqual([false, true, true]);
    for (const t of screen.getAllByRole("table")) expect(t.querySelectorAll("th").length).toBeGreaterThan(0);
  });
  // Five statuses are wider than a phone: there the status is a select under the toolbar.
  it("puts the status filter under the toolbar on a phone, and in it on a wider screen", async () => {
    listRuns.mockResolvedValue([run("r-1", "first", "s-1")]);
    const toolbarHasStatuses = async () => {
      await screen.findByRole("button", { name: "first" });
      const bar = document.querySelector<HTMLElement>('search[aria-label="Search and filters"]')!;
      expect(screen.getByLabelText("Filter runs by status")).toBeInTheDocument();
      return within(bar).queryByLabelText("Filter runs by status") !== null;
    };
    render(<Runs />);
    expect(await toolbarHasStatuses()).toBe(true);
    cleanup();
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === "(max-width: 639px)",
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    try {
      render(<Runs />);
      expect(await toolbarHasStatuses()).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
