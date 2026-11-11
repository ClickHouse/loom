import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { RecallLab } from "./RecallLab";
import { must } from "../test/must";

// Recall history honours the run a replay opened it for, asks the server for that run
// over a 30-day window, and clears with a chip.

const listSearchTraceLog = vi.fn<typeof import("../api").listSearchTraceLog>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listSearchTraceLog: (...args: Parameters<typeof actual.listSearchTraceLog>) => listSearchTraceLog(...args),
    searchMemory: vi.fn(async () => ({ results: [], embedding_model: "" })),
    isAuthDisabled: () => false,
  };
});

describe("Recall history run filter", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listSearchTraceLog.mockResolvedValue({ rows: [] });
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
      recallRunFilter: "r-full",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, recallRunFilter: null });
  });

  it("lands on the history tab and asks for the run's searches over 30 days, then clears the filter from the chip", async () => {
    render(<RecallLab />);
    // No tab click: the replay link must land on history by itself.
    await waitFor(() => expect(listSearchTraceLog).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Searches under run")).toBeInTheDocument();
    const [, opts] = listSearchTraceLog.mock.calls[0];
    expect(opts?.run_id).toBe("r-full");
    expect(opts?.since_hours).toBe(720);
    expect(await screen.findByText("No recorded searches under this run")).toBeInTheDocument();
    expect(screen.getByText(/the agent tags it with the run/)).toBeInTheDocument();
    expect(screen.queryByText(/X-Loom-Run-Id|run_id/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove r-full" }));
    await waitFor(() => expect(listSearchTraceLog).toHaveBeenCalledTimes(2));
    const opts2 = must(listSearchTraceLog.mock.calls[1][1], "second trace-log opts");
    expect(opts2.run_id).toBeUndefined();
    expect(useStore.getState().recallRunFilter).toBeNull();
  });

  it("opens on the score tab when no run is set, and moves to history when a run arrives", async () => {
    useStore.setState({ recallRunFilter: null });
    render(<RecallLab />);
    expect(listSearchTraceLog).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText(/scoring breakdown/i)).toBeInTheDocument();
    useStore.getState().openRecallForRun("r-late");
    await waitFor(() => expect(listSearchTraceLog).toHaveBeenCalledTimes(1));
    const [, opts] = listSearchTraceLog.mock.calls[0];
    expect(opts?.run_id).toBe("r-late");
    expect(screen.getByText("Searches under run")).toBeInTheDocument();
  });
});
