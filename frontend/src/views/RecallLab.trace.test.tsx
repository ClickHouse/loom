import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHit } from "../api";
import { useStore } from "../lib/state";
import { RecallLab } from "./RecallLab";

// A scored query whose answer lands after the reader typed the next one is dropped: the
// hits on screen always belong to the query in the box, and "Scoring…" stays up until the
// current query answers.

const searchMemory = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    searchMemory: (...args: unknown[]) => searchMemory(...args),
    listMemories: vi.fn(async () => ({ entries: [], total: 0, today: 0, truncated: false })),
    listSearchTraceLog: vi.fn(async () => ({ rows: [] })),
    isAuthDisabled: () => false,
  };
});

type Trace = { results: SearchHit[]; embedding_model: string; abstain?: boolean };
function deferred() {
  let resolve: (v: Trace) => void = () => {};
  const promise = new Promise<Trace>((r) => (resolve = r));
  return { promise, resolve };
}

const hit = (key: string, extra: Partial<SearchHit> = {}): SearchHit => ({
  memory_key: key,
  memory_type: "semantic",
  content_excerpt: `excerpt of ${key}`,
  distance: 0.1,
  rank_score: 0.9,
  confidence: 1,
  source: "fact",
  ...extra,
});

describe("Recall Lab: score a query", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
      recallRunFilter: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("drops the first query's answer when it lands after the second query's", async () => {
    const first = deferred();
    const second = deferred();
    searchMemory.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<RecallLab />);
    const box = screen.getByLabelText("Recall query");
    fireEvent.change(box, { target: { value: "deploy" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(1));
    fireEvent.change(box, { target: { value: "deploy rollback" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(2));
    expect(searchMemory.mock.calls[1][1]).toBe("deploy rollback");

    second.resolve({ results: [hit("rollback_runbook")], embedding_model: "m" });
    expect(await screen.findByText("excerpt of rollback_runbook")).toBeInTheDocument();
    first.resolve({ results: [hit("deploy_note")], embedding_model: "m" });
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("excerpt of deploy_note")).toBeNull();
    expect(screen.getByText("excerpt of rollback_runbook")).toBeInTheDocument();
  });

  it("keeps Scoring… up while the current query is in flight, even when the superseded one answers", async () => {
    const first = deferred();
    const second = deferred();
    searchMemory.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<RecallLab />);
    const box = screen.getByLabelText("Recall query");
    fireEvent.change(box, { target: { value: "deploy" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(1));
    fireEvent.change(box, { target: { value: "deploy rollback" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(2));
    first.resolve({ results: [hit("deploy_note")], embedding_model: "m" });
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.getByText("Scoring…")).toBeInTheDocument();
    expect(screen.queryByText("excerpt of deploy_note")).toBeNull();
    second.resolve({ results: [hit("rollback_runbook")], embedding_model: "m" });
    expect(await screen.findByText("excerpt of rollback_runbook")).toBeInTheDocument();
    expect(screen.queryByText("Scoring…")).toBeNull();
  });

  it("clearing the box mid-flight ends Scoring… and shows no late hits", async () => {
    const first = deferred();
    searchMemory.mockReturnValueOnce(first.promise);
    render(<RecallLab />);
    const box = screen.getByLabelText("Recall query");
    fireEvent.change(box, { target: { value: "deploy" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Scoring…")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "" } });
    await waitFor(() => expect(screen.queryByText("Scoring…")).toBeNull());
    first.resolve({ results: [hit("deploy_note")], embedding_model: "m" });
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("excerpt of deploy_note")).toBeNull();
  });

  it("ranks like the Memories list and names the term that moved a hit", async () => {
    searchMemory.mockResolvedValueOnce({
      results: [hit("finding_1", { distance: 0.67, rank_score: 1.2, score_components: { key_match: 0.85 } })],
      embedding_model: "m",
    });
    render(<RecallLab />);
    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "finding_1" } });
    expect(await screen.findByText("excerpt of finding_1")).toBeInTheDocument();
    expect(searchMemory.mock.calls[0][2]).toMatchObject({ explain: true, legs: ["memory"] });
    expect(screen.getByText("key match +0.85")).toBeInTheDocument();
    expect(screen.getByText("sim 0.330")).toBeInTheDocument();
  });

  it("folds a result set the search abstained on until the reader asks", async () => {
    searchMemory.mockResolvedValueOnce({ results: [hit("weak")], embedding_model: "m", abstain: true });
    render(<RecallLab />);
    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "nothing like it" } });
    fireEvent.click(await screen.findByText("Show 1 anyway"));
    expect(screen.getByText("excerpt of weak")).toBeInTheDocument();
  });
});
