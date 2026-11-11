import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, RunListOptions, RunListPage, RunRow } from "../api";
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
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
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

  const unfoldedReads = () => listRunsPage.mock.calls.filter(([, o]) => o?.memoryActivity === "");

  it("with no run at all says No runs yet and offers Connect, after one unfolded read", async () => {
    listRunsPage.mockResolvedValue({ runs: [], total: 0, paged: true });
    render(<Runs />);
    expect(await screen.findByRole("heading", { name: "No runs yet" })).toBeInTheDocument();
    expect(
      screen.getByText(/A run is one session of an agent's work: what it read and what it wrote/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/run\.begin|run_id|memory\.set|run\.complete/)).toBeNull();
    expect(screen.queryByRole("heading", { name: "No runs with memory activity" })).toBeNull();
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    // One read of a single run, with no memory-activity filter, told the two apart.
    expect(unfoldedReads()).toHaveLength(1);
    expect(unfoldedReads()[0][1]).toMatchObject({ limit: 1, memoryActivity: "" });
  });

  it("with runs that are all folded says so, unfolds them, and reads no more than once", async () => {
    listRunsPage.mockImplementation(async (_id, o) =>
      o?.memoryActivity === "with" ? { runs: [], total: 0, paged: true } : { runs: [run(1)], total: 1, paged: true },
    );
    render(<Runs />);
    expect(await screen.findByRole("heading", { name: "No runs with memory activity" })).toBeInTheDocument();
    expect(screen.getByText(/No run with memory activity yet\. Runs without any are hidden\./)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "No runs yet" })).toBeNull();
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeInTheDocument();
    expect(unfoldedReads()).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Show runs with no memory activity" }));
    expect(await screen.findByText("Triage lane 1")).toBeInTheDocument();
    // The unfold left with its state: focus is on the toolbar's toggle, now checked.
    const toggle = screen.getByRole("checkbox", { name: "Show runs with no memory activity" });
    expect(toggle).toHaveFocus();
    expect(toggle).toBeChecked();
  });

  it("offers a viewer no connect, in either empty state", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "v@x", org: "dev", roles: ["VIEWER"], namespaces: ["ns"] },
      myNamespaces: [{ namespace: "ns", role: "viewer", status: "active" } as MyNamespaceEntry],
    });
    listRunsPage.mockResolvedValue({ runs: [], total: 0, paged: true });
    render(<Runs />);
    expect(await screen.findByRole("heading", { name: "No runs yet" })).toBeInTheDocument();
    expect(screen.getByText(/when someone with writer access connects an agent that starts one/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect an agent" })).toBeNull();
    cleanup();
    listRunsPage.mockImplementation(async (_id, o) =>
      o?.memoryActivity === "with" ? { runs: [], total: 0, paged: true } : { runs: [run(1)], total: 1, paged: true },
    );
    render(<Runs />);
    expect(await screen.findByRole("heading", { name: "No runs with memory activity" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show runs with no memory activity" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect an agent" })).toBeNull();
  });
});
