import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchTraceLogRow } from "../api";
import { useStore } from "../lib/state";
import { RecallLab, pairKeysWithScores } from "./RecallLab";

// The Recall history row shows every returned memory WITH its score, the whole query as
// it was sent, and which op sent it, so "what did my agent retrieve, and how sure was it
// about each" is answered from the page.

const listSearchTraceLog = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listSearchTraceLog: (...args: unknown[]) => listSearchTraceLog(...args),
    searchMemory: vi.fn(async () => ({ results: [], embedding_model: "" })),
    isAuthDisabled: () => false,
  };
});

const KEYS = Array.from({ length: 10 }, (_, i) => `finding_${i}`);
const SCORES = KEYS.map((_, i) => 0.9 - i * 0.05);
const LONG_QUERY = "src Interpreters executeQuery cpp ".repeat(20).trim();

const row = (over: Partial<SearchTraceLogRow> = {}): SearchTraceLogRow => ({
  ts: "2026-09-21T07:00:00Z",
  agent: "reporter",
  query: "uniqTheta state blob allocates 1 GiB",
  returned_keys: KEYS,
  returned_scores: SCORES,
  top_k: 20,
  result_count: 10,
  abstain: false,
  retrieval_risk: "low",
  embedding_model: "m",
  latency_ms: 695,
  run_id: "-",
  operation: "memory.ask",
  ...over,
});

async function openHistory() {
  render(<RecallLab />);
  await userEvent.click(screen.getByRole("tab", { name: /Recall history/ }));
  await waitFor(() => expect(listSearchTraceLog).toHaveBeenCalledTimes(1));
}

describe("pairKeysWithScores", () => {
  it("pairs by position and leaves a key without a score when the arrays differ", () => {
    expect(pairKeysWithScores(["a", "b", "c"], [0.9, 0.4])).toEqual([
      { key: "a", score: 0.9 },
      { key: "b", score: 0.4 },
      { key: "c", score: null },
    ]);
  });
});

describe("Recall history rows", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("shows each returned key with its own score, folded at eight, and unfolds to all", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row()] });
    await openHistory();
    const list = await screen.findByRole("list", { name: "Returned memories with scores" });
    expect(list).toBeInTheDocument();
    // Every shown key sits beside its own score, not the top score.
    expect(screen.getByText("finding_0")).toBeInTheDocument();
    expect(screen.getByText("0.900")).toBeInTheDocument();
    expect(screen.getByText("finding_7")).toBeInTheDocument();
    expect(screen.getByText("0.550")).toBeInTheDocument();
    expect(screen.queryByText("finding_8")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all 10" }));
    expect(screen.getByText("finding_9")).toBeInTheDocument();
    expect(screen.getByText("0.450")).toBeInTheDocument();
  });

  it("says in plain words how the search was asked", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row({ operation: "context.pack" })] });
    await openHistory();
    expect(await screen.findByText("via session context")).toBeInTheDocument();
  });

  it("calls an operation it has no words for another call, and names it in the tooltip", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row({ operation: "facts.search" })] });
    await openHistory();
    const badge = await screen.findByText("via another call");
    const target = badge.closest<HTMLElement>('[role="note"]');
    if (!target) throw new Error("the badge has no tooltip target");
    act(() => target.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("(facts.search)");
  });

  it("omits the operation chip on a row written before the column existed", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row({ operation: "" })] });
    await openHistory();
    await screen.findByText("finding_0");
    expect(screen.queryByText(/^via /)).toBeNull();
  });

  it("folds a long query behind a toggle and shows the whole text on request", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row({ query: LONG_QUERY })] });
    await openHistory();
    const toggle = await screen.findByRole("button", { name: /Show full query/ });
    const text = screen.getByText(LONG_QUERY);
    expect(text.className).toContain("line-clamp-3");
    fireEvent.click(toggle);
    expect(screen.getByText(LONG_QUERY).className).not.toContain("line-clamp-3");
    expect(screen.getByRole("button", { name: "Show less" })).toBeInTheDocument();
  });

  it("a short query has no toggle", async () => {
    listSearchTraceLog.mockResolvedValue({ rows: [row()] });
    await openHistory();
    await screen.findByText("finding_0");
    expect(screen.queryByRole("button", { name: /Show full query/ })).toBeNull();
  });

  it("a key without a recorded score shows a dash, never a neighbour's number", async () => {
    listSearchTraceLog.mockResolvedValue({
      rows: [row({ returned_keys: ["a", "b"], returned_scores: [0.7], result_count: 2 })],
    });
    await openHistory();
    await screen.findByText("a");
    expect(screen.getByText("0.700")).toBeInTheDocument();
    expect(screen.getByText("–")).toBeInTheDocument();
  });
});

// A failed read is the list's error state, with its request id and a Retry that reads again.
describe("Recall history errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
    });
  });
  afterEach(() => cleanup());

  it("says the read failed, with a Retry that reads again", async () => {
    const { ApiError } = await import("../api");
    listSearchTraceLog.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "upstream down", requestId: "req-7" }),
    );
    listSearchTraceLog.mockResolvedValue({ rows: [row()] });
    await openHistory();
    expect(await screen.findByText("Couldn't load recorded searches")).toBeInTheDocument();
    expect(screen.getByText(/req-7/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(listSearchTraceLog).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("uniqTheta state blob allocates 1 GiB")).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load recorded searches")).toBeNull();
  });
});
