import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunListOptions, RunListPage, RunRow } from "../api";
import { ApiError } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// run.list pages, sorts and searches on the server: the page asks for one page at a time
// (offset, limit, order_by, q) and shows the server's total in the toolbar and the pager.
// A server a release behind sends no total: the pager then stops on a short page.

const listRunsPage = vi.fn<(id: unknown, opts?: RunListOptions) => Promise<RunListPage>>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listRunsPage: (id: unknown, opts?: RunListOptions) => listRunsPage(id, opts),
    getRun: vi.fn(async () => ({ found: false, run: null })),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const run = (i: number, over: Partial<RunRow> = {}): RunRow => ({
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
  ...over,
});

/** A server holding `n` runs: it answers the page the request names, with the total. */
function server(n: number, { total = true }: { total?: boolean } = {}) {
  listRunsPage.mockImplementation(async (_id, opts = {}) => {
    const all = Array.from({ length: n }, (_, i) => run(i + 1)).filter(
      (r) => !opts.q || r.goal.includes(opts.q.toLowerCase()),
    );
    const from = opts.offset ?? 0;
    return { runs: all.slice(from, from + (opts.limit ?? 50)), total: total ? all.length : null };
  });
}

const lastOpts = () => listRunsPage.mock.calls.at(-1)?.[1] ?? {};
const toolbar = () => document.querySelector<HTMLElement>('search[aria-label="Search and filters"]')!;
const bodyRows = () =>
  within(screen.getByRole("table", { name: "Runs" }))
    .getAllByRole("row")
    .slice(1);

describe("Runs paging, sorting and search on the server", () => {
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
    vi.useRealTimers();
    useStore.setState({ identity: null });
  });

  it("asks for one page and counts the server's total", async () => {
    server(1234);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(lastOpts()).toMatchObject({ limit: 50, offset: 0, orderBy: "started_desc" });
    expect(bodyRows()).toHaveLength(50);
    expect(toolbar()).toHaveTextContent("1–50 of 1,234");
    expect(screen.getByRole("navigation", { name: "Runs pages" })).toHaveTextContent("1–50 of 1,234");

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByRole("button", { name: "goal 51" })).toBeInTheDocument();
    expect(lastOpts()).toMatchObject({ limit: 50, offset: 50 });
    expect(toolbar()).toHaveTextContent("51–100 of 1,234");
    expect(document.getElementById("runs-list")).toHaveFocus();
  });

  it("asks for the page size picked", async () => {
    localStorage.setItem("loom_page_size_runs_v1", "25");
    server(100);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(lastOpts()).toMatchObject({ limit: 25, offset: 0 });
    expect(bodyRows()).toHaveLength(25);
  });

  // Review finding: a server a release behind ignores offset, order_by and q and answers its
  // newest runs (at most 200) with no total. Then the page reads those once and pages,
  // searches and sorts them here, instead of showing page 1 again on every Next.
  it("against a server that does not page, pages, searches and sorts its newest runs here", async () => {
    listRunsPage.mockImplementation(async (_id, opts = {}) => ({
      runs: Array.from({ length: Math.min(opts.limit ?? 50, 200) }, (_, i) =>
        run(i + 1, { duration_ms: i * 10 }),
      ).slice(0, 120),
      total: null,
    }));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    expect(lastOpts().offset ?? 0).toBe(0);
    expect(toolbar()).toHaveTextContent("1–50 of 120");
    const pager = screen.getByRole("navigation", { name: "Runs pages" });
    fireEvent.click(within(pager).getByRole("button", { name: "Next page" }));
    expect(await screen.findByRole("button", { name: "goal 51" })).toBeInTheDocument();
    expect(lastOpts().offset ?? 0).toBe(0);
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "goal 11" } });
    await screen.findByRole("button", { name: "goal 110" });
    expect(bodyRows().length).toBeLessThan(15);
    expect(screen.queryByRole("button", { name: "goal 2" })).toBeNull();
  });

  it("sends the search to the server after 250 ms and starts again from page 1", async () => {
    server(120);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    const calls = listRunsPage.mock.calls.length;
    vi.useFakeTimers();
    fireEvent.change(screen.getByRole("textbox", { name: "Search runs" }), { target: { value: "GOAL 11" } });
    act(() => vi.advanceTimersByTime(200));
    expect(listRunsPage.mock.calls.length).toBe(calls);
    act(() => vi.advanceTimersByTime(60));
    vi.useRealTimers();
    await waitFor(() => expect(lastOpts()).toMatchObject({ q: "GOAL 11", offset: 0 }));
    // goal 11 and goal 110 to goal 119.
    await waitFor(() => expect(bodyRows()).toHaveLength(11));
    expect(toolbar()).toHaveTextContent("11 matches");
  });

  it("sorts by a column header, one sort at a time, and says so on the header", async () => {
    server(120);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    const header = (name: string) => screen.getByRole("columnheader", { name });
    expect(header("Started")).toHaveAttribute("aria-sort", "descending");
    expect(header("Took")).toHaveAttribute("aria-sort", "none");
    expect(header("Goal")).not.toHaveAttribute("aria-sort");

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    fireEvent.click(within(header("Took")).getByRole("button", { name: "Took" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ orderBy: "duration_desc", offset: 0 }));
    expect(header("Took")).toHaveAttribute("aria-sort", "descending");
    expect(header("Started")).toHaveAttribute("aria-sort", "none");

    fireEvent.click(within(header("Took")).getByRole("button", { name: "Took" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ orderBy: "duration_asc" }));
    expect(header("Took")).toHaveAttribute("aria-sort", "ascending");

    fireEvent.click(within(header("Relied on")).getByRole("button", { name: "Relied on" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ orderBy: "relied_on_desc" }));
    fireEvent.click(within(header("Started")).getByRole("button", { name: "Started" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ orderBy: "started_desc" }));
  });

  it("sends the status filter and starts again from page 1", async () => {
    server(120);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    fireEvent.click(within(screen.getByLabelText("Filter runs by status")).getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ status: "failed", offset: 0 }));
  });

  it("keeps the rows on screen, marked busy, while the next page loads", async () => {
    server(120);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    listRunsPage.mockReturnValue(new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Runs" })).toHaveAttribute("aria-busy", "true");
    expect(toolbar()).toHaveTextContent("1–50 of 120");
  });

  it("has no summary line beside the toggles", async () => {
    server(30);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(screen.queryByText(/memories relied on/)).toBeNull();
    expect(screen.queryByText(/Newest 200 runs/)).toBeNull();
  });

  it("says a failed read failed, with its request id and a Retry, and no count", async () => {
    listRunsPage.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "upstream down", requestId: "req-42" }),
    );
    server(3);
    render(<Runs />);
    expect(await screen.findByText("Couldn't load runs")).toBeInTheDocument();
    expect(screen.getByText(/req-42/)).toBeInTheDocument();
    expect(screen.queryByText("No runs yet")).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/\d/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
  });

  it("says no run matches the search, with a way to clear it", async () => {
    server(3);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.change(screen.getByRole("textbox", { name: "Search runs" }), { target: { value: "nothing" } });
    expect(await screen.findByText(/No runs match/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search runs" })).toHaveValue("");
  });

  it("draws the first read as Click UI's loading table, marked busy", async () => {
    listRunsPage.mockReturnValue(new Promise(() => {}));
    render(<Runs />);
    const table = await screen.findByRole("table", { name: "Runs" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(table).getByText("Loading data")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Goal" })).toBeInTheDocument();
  });

  it("keeps the description to one sentence and the rest in its help", async () => {
    server(1);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(screen.getByRole("button", { name: "About runs" })).toBeInTheDocument();
    expect(screen.queryByText(/group by it to see a whole working sitting/)).toBeNull();
  });
});
