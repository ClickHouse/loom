import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunListOptions, RunListPage, RunRow } from "../api";
import { ApiError } from "../api";
import { useStore } from "../lib/state";
import { Runs } from "./Runs";

// run.list pages, sorts and searches on the server: the page asks for one page at a time
// (offset, limit, order_by, q) and shows the server's total in the toolbar and the pager.
// A server a release behind sends no total: the page then reads its newest 200 runs once and
// pages, searches and sorts them here.

// A 50-row Click UI Table renders in about half a second in jsdom, and the paging, search
// and sort cases render several: about 2 s alone, past the 5 s default under a full parallel
// run on a loaded host.
vi.setConfig({ testTimeout: 15_000 });

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
  // Run 1 is the newest, a minute apart, as run.list orders them.
  started_at: new Date(Date.UTC(2026, 8, 20, 7) - i * 60_000).toISOString(),
  completed_at: "2026-09-20T07:05:00Z",
  duration_ms: 1000,
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

/** A server holding `n` runs: it answers the page the request names, with the total on the
 *  first page, the last and one past the end, as run.list does; a full later page has none. */
function server(n: number, { total = true }: { total?: boolean } = {}) {
  listRunsPage.mockImplementation(async (_id, opts = {}) => {
    const all = Array.from({ length: n }, (_, i) => run(i + 1)).filter(
      (r) => !opts.q || r.goal.includes(opts.q.toLowerCase()),
    );
    const from = opts.offset ?? 0;
    const runs = all.slice(from, from + (opts.limit ?? 50));
    const counted = from === 0 || runs.length < (opts.limit ?? 50);
    return { runs, total: total && counted ? all.length : null, paged: total };
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

  it("asks for one page, and keeps the first page's total on the next", async () => {
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

  // The rows on screen while a search reads are the unfiltered page: never counted as matches.
  it("counts no matches until the search's own read answers", async () => {
    server(25);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    let answer: (v: never) => void = () => {};
    listRunsPage.mockImplementationOnce(() => new Promise((r) => (answer = r as (v: never) => void)));
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "goal 2" } });
    await waitFor(() => expect(lastOpts()).toMatchObject({ q: "goal 2" }));
    expect(toolbar()).not.toHaveTextContent("matches");
    await act(async () => answer({ runs: [run(2)], total: 1, paged: true } as never));
    await waitFor(() => expect(toolbar()).toHaveTextContent("1 match"));
  });

  it("asks for the page size picked", async () => {
    localStorage.setItem("loom_page_size_runs_v1", "25");
    server(100);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(lastOpts()).toMatchObject({ limit: 25, offset: 0 });
    expect(bodyRows()).toHaveLength(25);
  });

  // A server a release behind ignores offset, order_by and q and answers its newest runs (at
  // most 200) with no total. The page reads those once and pages, searches and sorts them
  // here, so Next does not show page 1 again.
  it("against a server that does not page, pages, searches and sorts its newest runs here", async () => {
    listRunsPage.mockImplementation(async (_id, opts = {}) => ({
      runs: Array.from({ length: Math.min(opts.limit ?? 50, 200) }, (_, i) =>
        run(i + 1, { duration_ms: i * 10 }),
      ).slice(0, 120),
      total: null,
      paged: false,
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

  // Such a server answers at most its newest 200: the page says it shows the newest 200 and
  // counts no total, never "of 200" as if that were every run.
  it("against a server that does not page and holds more, says it shows the newest 200", async () => {
    listRunsPage.mockImplementation(async (_id, opts = {}) => ({
      runs: Array.from({ length: Math.min(opts.limit ?? 50, 200) }, (_, i) => run(i + 1)),
      total: null,
      paged: false,
    }));
    render(<Runs />);
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    expect(await screen.findByText(/Showing the newest 200 runs/)).toBeInTheDocument();
    expect(toolbar()).toHaveTextContent("1–50");
    expect(toolbar()).not.toHaveTextContent(/of 200/);
  });

  it("says nothing of a cap when such a server holds fewer than 200", async () => {
    listRunsPage.mockImplementation(async () => ({ runs: [run(1), run(2)], total: null, paged: false }));
    render(<Runs />);
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    await screen.findByRole("button", { name: "goal 2" });
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });

  // Another namespace leaves the local mode and asks for a page first.
  it("reads another namespace's runs a page at a time again when its server pages", async () => {
    listRunsPage.mockImplementation(async (id) =>
      (id as { namespace: string }).namespace === "ns"
        ? { runs: [run(1)], total: null, paged: false }
        : { runs: [run(7)], total: 1, paged: true },
    );
    render(<Runs />);
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    await screen.findByRole("button", { name: "goal 1" });
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ns2", agent: "loom-ui" } }));
    await screen.findByRole("button", { name: "goal 7" });
    expect(lastOpts()).toMatchObject({ limit: 50, offset: 0, orderBy: "started_desc" });
  });

  // The reset happens during render, so not even the new namespace's first read carries the
  // old page or agent.
  it("asks another namespace for its first page, for every agent, in its first read", async () => {
    server(120);
    // Two agents, so the page offers the agent filter; the server ignores it.
    const answer = listRunsPage.getMockImplementation()!;
    listRunsPage.mockImplementation(async (id, opts) => {
      const p = await answer(id, opts);
      return { ...p, runs: p.runs.map((r, i) => (i === 0 ? { ...r, agent: "reporter" } : r)) };
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    await userEvent.click(await screen.findByRole("button", { name: "Agent" }));
    // Click UI's Select items carry no option role; they are addressed by value.
    await userEvent.click(document.querySelector<HTMLElement>('[cui-select-item][data-value="claude-code"]')!);
    await waitFor(() => expect(lastOpts()).toMatchObject({ agent: "claude-code", offset: 0 }));
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    expect(lastOpts()).toMatchObject({ agent: "claude-code", offset: 50 });
    act(() => useStore.setState({ identity: { org: "dev", namespace: "ns2", agent: "loom-ui" } }));
    await waitFor(() =>
      expect(listRunsPage.mock.calls.some(([id]) => (id as { namespace: string }).namespace === "ns2")).toBe(true),
    );
    const first = listRunsPage.mock.calls.find(([id]) => (id as { namespace: string }).namespace === "ns2");
    expect(first?.[1]).toMatchObject({ offset: 0, agent: "" });
  });

  it("stays local when a later read carries a total, until the namespace changes", async () => {
    let upgraded = false;
    listRunsPage.mockImplementation(async () =>
      upgraded ? { runs: [run(3)], total: 1, paged: true } : { runs: [run(1)], total: null, paged: false },
    );
    render(<Runs />);
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    await screen.findByRole("button", { name: "goal 1" });
    upgraded = true;
    fireEvent.click(within(screen.getByLabelText("Filter runs by status")).getByRole("button", { name: "Failed" }));
    await screen.findByRole("button", { name: "goal 3" });
    expect(lastOpts()).toMatchObject({ status: "failed", limit: 200 });
    expect(lastOpts()).not.toHaveProperty("offset");
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
    // The typed words, bare, among the runs the default fold keeps.
    expect(
      await screen.findByText((_, el) => el?.textContent === "No runs with memory activity match nothing"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search runs" })).toHaveValue("");
  });

  it("with every run shown, says no run matches the words", async () => {
    server(3);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(screen.getByText("Show runs with no memory activity"));
    fireEvent.change(screen.getByRole("textbox", { name: "Search runs" }), { target: { value: "nothing" } });
    expect(await screen.findByText((_, el) => el?.textContent === "No runs match nothing")).toBeInTheDocument();
  });

  it("with a status picked too, says the filters in force match nothing, not the words", async () => {
    server(3);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(
      within(screen.getByRole("group", { name: "Filter runs by status" })).getByRole("button", { name: "Failed" }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Search runs" }), { target: { value: "nothing" } });
    expect(
      await screen.findByText((_, el) => el?.textContent === "No runs with memory activity match the filters in force"),
    ).toBeInTheDocument();
  });

  it("draws the first read as the table's header over a skeleton, marked busy", async () => {
    listRunsPage.mockReturnValue(new Promise(() => {}));
    render(<Runs />);
    const table = await screen.findByRole("table", { name: "Runs" });
    expect(table).toHaveAttribute("aria-busy", "true");
    // The first read waits 300 ms before its loader shows, like every loader
    // (ui/LoadingSkeleton), under the header row already in place.
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading runs" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Goal" })).toBeInTheDocument();
  });

  it("keeps the description to one sentence and the rest in its help", async () => {
    server(1);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(screen.getByRole("button", { name: "About runs" })).toBeInTheDocument();
    expect(screen.queryByText(/group by it to see a whole working sitting/)).toBeNull();
  });

  // "No runs match" is said only of a read of the current filter, not of the
  // previous filter's empty answer while the new one is in flight.
  it("does not say a new search matches nothing before its answer lands", async () => {
    let answer: (p: RunListPage) => void = () => {};
    listRunsPage.mockImplementation(async (_id, opts = {}) =>
      opts.q === "goal"
        ? new Promise<RunListPage>((r) => (answer = r))
        : { runs: opts.q ? [] : [run(1)], total: opts.q ? 0 : 1, paged: true },
    );
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "zzz" } });
    await screen.findByText(/^No runs (with memory activity )?match/);
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "goal" } });
    await waitFor(() => expect(lastOpts()).toMatchObject({ q: "goal" }));
    expect(screen.queryByText(/^No runs (with memory activity )?match/)).toBeNull();
    answer({ runs: [run(1)], total: 1, paged: true });
    await screen.findByRole("button", { name: "goal 1" });
  });

  // With a release and the one before it behind one URL, answers alternate between no total
  // and a total. The list settles in the local mode and shows the rows.
  it("settles on a mixed fleet, whose answers alternate between no total and a total", async () => {
    let n = 0;
    listRunsPage.mockImplementation(async (_id, opts = {}) => {
      n += 1;
      const runs = Array.from({ length: Math.min(opts.limit ?? 50, 120) }, (_, i) => run(i + 1));
      return { runs, total: n % 2 === 1 ? null : 120, paged: n % 2 === 0 };
    });
    render(<Runs />);
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(listRunsPage).toHaveBeenCalledTimes(2);
    expect(lastOpts()).toMatchObject({ limit: 200 });
    // In the local mode the next page is read from the rows already held.
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Runs pages" })).getByRole("button", { name: "Next page" }),
    );
    expect(await screen.findByRole("button", { name: "goal 51" })).toBeInTheDocument();
    expect(listRunsPage).toHaveBeenCalledTimes(2);
  });

  // A paged list that meets a server with no total mid-session (a new filter answered by the
  // release before) shows neither that answer nor the previous filter's rows while the newest
  // 200 are read: only the loader, and no count.
  it("shows the loader, no rows and no count while it switches to the local mode", async () => {
    let release: (v: RunListPage) => void = () => {};
    listRunsPage.mockImplementation(async (_id, opts = {}) => {
      if (opts.limit === 200) return new Promise<RunListPage>((r) => (release = r));
      const runs = Array.from({ length: 50 }, (_, i) => run(i + 1));
      return { runs, total: opts.status ? null : 120, paged: !opts.status };
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    expect(toolbar()).toHaveTextContent("1–50 of 120");
    fireEvent.click(within(screen.getByLabelText("Filter runs by status")).getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ status: "failed", limit: 200 }));
    expect(screen.queryByRole("button", { name: /^goal / })).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/\d–\d/);
    const table = screen.getByRole("table", { name: "Runs" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(await within(table).findByRole("status", { name: "Loading runs" })).toBeInTheDocument();
    act(() => release({ runs: [run(7, { status: "failed" })], total: null, paged: false }));
    expect(await screen.findByRole("button", { name: "goal 7" })).toBeInTheDocument();
    expect(toolbar()).toHaveTextContent("1–1");
  });

  // run.list serves no offset past 100,000, so the pager offers the pages it can serve and
  // says where it stops.
  it("offers only the pages run.list can serve when it counts more", async () => {
    listRunsPage.mockImplementation(async () => ({
      runs: Array.from({ length: 50 }, (_, i) => run(i + 1)),
      total: 1_000_000,
      paged: true,
    }));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    const nav = screen.getByRole("navigation", { name: "Runs pages" });
    expect(within(nav).getByText("1–50 of 1,000,000 rows")).toBeInTheDocument();
    expect(within(nav).getByText("Pages 1–2,001 can be opened.")).toBeInTheDocument();
  });

  // Runs that left the filter while the next page was read: its read comes back empty with a
  // smaller total, and the list steps back to the last page instead of showing nothing.
  it("steps back to the last page when the next page's read finds it gone", async () => {
    let shrunk = false;
    listRunsPage.mockImplementation(async (_id, opts = {}) => {
      const total = shrunk ? 45 : 60;
      const from = opts.offset ?? 0;
      return {
        runs: Array.from({ length: Math.max(0, Math.min(50, total - from)) }, (_, i) => run(from + i + 1)),
        total,
        paged: true,
      };
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    shrunk = true;
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Runs pages" })).getByRole("button", { name: "Next page" }),
    );
    await waitFor(() => expect(lastOpts()).toMatchObject({ offset: 0 }));
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(toolbar()).toHaveTextContent("1–45 of 45");
    expect(within(screen.getByRole("navigation", { name: "Runs pages" })).getByRole("status")).toHaveTextContent(
      "Page 1 of 1",
    );
  });

  // A new page size starts again from page 1, even from a later page whose rows are still on
  // screen: their page counts in the old size.
  it("starts again from page 1 when the page size changes on a later page", async () => {
    server(120);
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    const pager = () => screen.getByRole("navigation", { name: "Runs pages" });
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 51" });
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    await screen.findByRole("button", { name: "goal 101" });
    await userEvent.click(within(pager()).getByRole("button", { name: "Rows per page" }));
    await userEvent.click(await screen.findByText(/^100/));
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 100, offset: 0 }));
    // The size choice just used stays, with focus.
    expect(within(pager()).getByRole("button", { name: "Rows per page" })).toHaveFocus();
    expect(await screen.findByRole("button", { name: "goal 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "goal 101" })).toBeNull();
    expect(within(pager()).getByRole("status")).toHaveTextContent("100 rows per page, page 1");
  });

  // A filter's first page is read before the pager moves again: a Next before it answers
  // would open the new list's second page unseen.
  it("pages nowhere while a new filter's first page is read", async () => {
    let answer: (v: RunListPage) => void = () => {};
    listRunsPage.mockImplementation(async (_id, opts = {}) => {
      if (opts.status === "failed") return new Promise<RunListPage>((r) => (answer = r));
      const from = opts.offset ?? 0;
      return { runs: Array.from({ length: 50 }, (_, i) => run(from + i + 1)), total: 1200, paged: true };
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    fireEvent.click(within(screen.getByLabelText("Filter runs by status")).getByRole("button", { name: "Failed" }));
    await waitFor(() => expect(lastOpts()).toMatchObject({ status: "failed", offset: 0 }));
    // The rows on screen are the old filter's (the toolbar still counts them, marked busy):
    // no pager pages from them.
    expect(screen.queryByRole("navigation", { name: "Runs pages" })).toBeNull();
    act(() => answer({ runs: [run(777, { status: "failed" })], total: 1, paged: true }));
    expect(await screen.findByRole("button", { name: "goal 777" })).toBeInTheDocument();
  });

  // Local mode entered from a later page (the release before answered there): a search with
  // no match says so, rather than a blank page with no pager.
  it("says no runs match a search in local mode entered from a later page", async () => {
    let old = false;
    listRunsPage.mockImplementation(async (_id, opts = {}) => {
      const from = opts.offset ?? 0;
      const runs = Array.from({ length: Math.min(opts.limit ?? 50, 120 - from) }, (_, i) => run(from + i + 1));
      return { runs, total: old ? null : 120, paged: !old };
    });
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 1" });
    old = true;
    fireEvent.click(
      within(screen.getByRole("navigation", { name: "Runs pages" })).getByRole("button", { name: "Next page" }),
    );
    await waitFor(() => expect(lastOpts()).toMatchObject({ limit: 200 }));
    await screen.findByRole("button", { name: "goal 51" });
    fireEvent.change(screen.getByLabelText("Search runs"), { target: { value: "nothing like this" } });
    expect(await screen.findByText(/^No runs (with memory activity )?match/)).toBeInTheDocument();
  });

  // Runs that started together keep run.list's order: run_id in the direction of the sort.
  it("against a server that does not page, breaks a tie in start by run_id, newest first", async () => {
    const at = { started_at: "2026-09-20T07:00:00Z" };
    listRunsPage.mockImplementation(async () => ({
      runs: [run(1, at), run(2, at), run(3, at)],
      total: null,
      paged: false,
    }));
    render(<Runs />);
    await screen.findByRole("button", { name: "goal 3" });
    expect(bodyRows().map((r) => within(r).getByRole("button", { name: /^goal / }).textContent)).toEqual([
      "goal 3",
      "goal 2",
      "goal 1",
    ]);
  });
});
