import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, SearchHit } from "../api";
import { useStore } from "../lib/state";
import { RecallLab } from "./RecallLab";

// Recall Lab is a scoring tool, so its rows are a Click UI Table: the
// rank, the key, the value on one line, similarity, the score with Click UI's table bar,
// the entity boost and the type. A row opens the memory drawer; the row count and the
// embedder sit at the end of the toolbar; a failed read says so with a Retry.

const searchMemory = vi.fn();
const listMemories = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    searchMemory: (...args: unknown[]) => searchMemory(...args),
    listMemories: (...args: unknown[]) => listMemories(...args),
    listSearchTraceLog: vi.fn(async () => ({ rows: [] })),
    isAuthDisabled: () => false,
  };
});

const hit = (key: string, rank_score: number, extra: Partial<SearchHit> = {}): SearchHit => ({
  memory_key: key,
  memory_type: "semantic",
  content_excerpt: `excerpt of ${key}`,
  distance: 0.2,
  similarity: 0.8123,
  rank_score,
  confidence: 1,
  score_components: { semantic: 0.6, entity_ann_boost_effective: 0.06 },
  ...extra,
});

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "procedural",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "claude-code",
  value: `value of ${memory_key}`,
});

async function score(text = "deploy") {
  fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: text } });
  return screen.findByRole("table", { name: "Recall scores" }, { timeout: 3000 });
}

describe("Recall Lab: the scores table", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMemories.mockResolvedValue({ entries: [], total: 0, today: 0, truncated: false });
    useStore.setState({
      authed: true,
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
      recallRunFilter: null,
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, inspectedMemoryKey: null });
  });

  it("draws one row per result under the seven columns and the assistant's", async () => {
    searchMemory.mockResolvedValue({
      results: [hit("deploy_note", 0.9), hit("rollback_runbook", 0.45, { score_components: { semantic: 0.4 } })],
      embedding_model: "text-embedding-3-small",
    });
    render(<RecallLab />);
    const table = await score();
    const heads = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(heads).toEqual(["#", "Key", "Value", "Similarity", "Score", "Entity", "Type", "Assistant"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const first = within(rows[0]);
    expect(first.getByText("1")).toBeInTheDocument();
    expect(first.getByRole("button", { name: "deploy_note" })).toBeInTheDocument();
    expect(first.getByText("excerpt of deploy_note")).toBeInTheDocument();
    expect(first.getByText("0.812")).toBeInTheDocument();
    expect(first.getByText("0.900")).toBeInTheDocument();
    expect(first.getByText("+0.06")).toBeInTheDocument();
    expect(first.getByText("semantic +0.60")).toBeInTheDocument();
    expect(first.getByText("semantic")).toBeInTheDocument();
    // No entity term: a dash.
    expect(within(rows[1]).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("drops the old namespace's rows on a switch, so none opens under the new one", async () => {
    searchMemory.mockImplementation(async (id: { namespace: string }) =>
      id.namespace === "other" ? new Promise(() => {}) : { results: [hit("deploy_note", 0.9)], embedding_model: "m" },
    );
    render(<RecallLab />);
    const table = await score();
    expect(within(table).getByRole("button", { name: "deploy_note" })).toBeInTheDocument();
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    expect(screen.queryByRole("button", { name: "deploy_note" })).toBeNull();
  });

  it("keeps settled scores out of draft query edits and clears them with the query", async () => {
    const result = hit("deploy_note", 0.9);
    const readTerms = vi.fn(() => ({ semantic: 0.6, entity_ann_boost_effective: 0.06 }));
    Object.defineProperty(result, "score_components", { get: readTerms });
    searchMemory.mockResolvedValue({ results: [result], embedding_model: "m" });
    render(<RecallLab />);
    const table = await score();
    expect(readTerms).toHaveBeenCalled();
    readTerms.mockClear();

    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "deploy lane" } });
    expect(screen.getByLabelText("Recall query")).toHaveValue("deploy lane");
    expect(within(table).getByText("excerpt of deploy_note")).toBeInTheDocument();
    expect(readTerms).not.toHaveBeenCalled();
    expect(searchMemory).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "" } });
    expect(screen.queryByRole("table", { name: "Recall scores" })).toBeNull();
  });

  it("draws the score as Click UI's table bar, relative to the top score", async () => {
    searchMemory.mockResolvedValue({
      results: [hit("deploy_note", 0.8), hit("rollback_runbook", 0.4)],
      embedding_model: "m",
    });
    render(<RecallLab />);
    const table = await score();
    const bars = table.querySelectorAll<HTMLElement>(".recall-bar > span");
    expect([...bars].map((b) => b.style.width)).toEqual(["100%", "50%"]);
    expect(table.querySelector(".recall-bar")).toHaveAttribute("aria-hidden", "true");
    // The old full-width accent rule is gone.
    expect(document.querySelector(".bg-accent")).toBeNull();
  });

  it("opens the memory drawer from a row, by click or from its key button", async () => {
    searchMemory.mockResolvedValue({
      results: [hit("deploy_note", 0.9, { agent: "writer", user_id: "-", session_id: "-" })],
      embedding_model: "m",
    });
    render(<RecallLab />);
    const table = await score();
    fireEvent.click(within(table).getByText("excerpt of deploy_note"));
    expect(useStore.getState().inspectedMemoryKey).toBe("deploy_note");
    useStore.setState({ inspectedMemoryKey: null });
    fireEvent.click(within(table).getByRole("button", { name: "deploy_note" }));
    expect(useStore.getState().inspectedMemoryKey).toBe("deploy_note");
  });

  it("keeps chunk and shared hits visible without opening a namesake", async () => {
    searchMemory.mockResolvedValue({
      results: [
        hit("chunk:s:0", 0.9, { source: "chunk", agent: "writer", user_id: "u", session_id: "s" }),
        hit("definition", 0.8, { agent: "_shared", user_id: "-", session_id: "-" }),
      ],
    });
    render(<RecallLab />);
    const table = within(await score());
    expect(table.getByRole("button", { name: "chunk:s:0" })).toBeDisabled();
    expect(table.getByRole("button", { name: "definition" })).toBeDisabled();
    fireEvent.click(table.getByText("excerpt of definition"));
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
  });

  it("opens the selected namesake using its complete identity", async () => {
    searchMemory.mockResolvedValue({
      results: [
        hit("deploy_note", 0.9, { namespace: "ns", agent: "writer", user_id: "-", session_id: "-" }),
        hit("deploy_note", 0.8, { namespace: "other", agent: "writer", user_id: "u", session_id: "s" }),
        hit("deploy_note", 0.7, { namespace: "ns", agent: "writer", content_excerpt: "identity incomplete" }),
      ],
    });
    render(<RecallLab />);
    const table = await score();
    const [local, other, incomplete] = within(table).getAllByRole("button", { name: "deploy_note" });
    expect(incomplete).toBeDisabled();
    fireEvent.click(within(table).getByText("identity incomplete"));
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    fireEvent.click(other);
    expect(useStore.getState()).toMatchObject({
      inspectedMemoryKey: "deploy_note",
      inspectedAgent: "writer",
      inspectedOwner: { user_id: "u", session_id: "s" },
      inspectedScope: { namespace: "other", exact: true },
    });
    fireEvent.click(local);
    expect(useStore.getState()).toMatchObject({
      inspectedOwner: { user_id: "-", session_id: "-" },
      inspectedScope: { namespace: "ns", exact: true },
    });
  });

  it("marks a keyword row and gives it dashes where nothing ranked it", async () => {
    listMemories.mockResolvedValue({ entries: [entry("deploy_kw")], total: 1, today: 0, truncated: false });
    searchMemory.mockResolvedValue({ results: [], embedding_model: "m" });
    render(<RecallLab />);
    const table = await score();
    const row = within(within(table).getAllByRole("row")[1]);
    expect(row.getByText("keyword match")).toBeInTheDocument();
    expect(row.getByText("value of deploy_kw")).toBeInTheDocument();
    expect(row.getByText("procedural")).toBeInTheDocument();
    expect(row.getAllByText("—")).toHaveLength(3);
  });

  it("keeps guidance with the matching writer and session when keys are shared", async () => {
    listMemories.mockResolvedValue({
      entries: [{ ...entry("deploy_note"), user_id: "user", session_id: "one" }],
      total: 1,
      today: 0,
      truncated: false,
    });
    searchMemory.mockResolvedValue({
      results: [
        hit("deploy_note", 0.9, {
          agent: "claude-code",
          user_id: "user",
          session_id: "one",
          content_excerpt: "matching revision",
          use_guidance: { next_step: "inspect_only", reasons: [] },
        }),
        hit("deploy_note", 0.8, {
          agent: "claude-code",
          user_id: "user",
          session_id: "two",
          content_excerpt: "other session",
          use_guidance: { next_step: "validate_lead", reasons: [] },
        }),
        hit("deploy_note", 0.7, {
          agent: "other-agent",
          user_id: "user",
          session_id: "one",
          content_excerpt: "other writer",
          use_guidance: { next_step: "follow_if_applicable", reasons: [] },
        }),
      ],
      embedding_model: "m",
    });
    render(<RecallLab />);
    const rows = within(await score())
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row));
    expect(rows).toHaveLength(3);
    expect(rows[0].getByText("keyword match")).toBeInTheDocument();
    expect(rows[0].getByText("matching revision")).toBeInTheDocument();
    expect(rows[0].getByText("Suggested use: inspect only")).toBeInTheDocument();
    expect(rows[1].queryByText("keyword match")).toBeNull();
    expect(rows[1].getByText("Suggested use: validate lead")).toBeInTheDocument();
    expect(rows[2].getByText("Suggested use: follow if applicable")).toBeInTheDocument();
  });

  it("puts the row count and the embedder at the end of the toolbar", async () => {
    searchMemory.mockResolvedValue({
      results: [hit("a1", 0.9), hit("a2", 0.5)],
      embedding_model: "text-embedding-3-small",
    });
    render(<RecallLab />);
    await score();
    const bar = document.querySelector<HTMLElement>('search[aria-label="Score a query"]');
    expect(bar).toHaveTextContent("2 rows · text-embedding-3-small");
    expect(within(bar as HTMLElement).getByLabelText("Recall query")).toBeInTheDocument();
  });

  it("says a failed read with Retry, which asks again", async () => {
    searchMemory.mockRejectedValueOnce(new Error("search down"));
    render(<RecallLab />);
    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "deploy" } });
    expect(await screen.findByText("Couldn't load scores", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("No candidates")).toBeNull();
    searchMemory.mockResolvedValue({ results: [hit("deploy_note", 0.9)], embedding_model: "m" });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table", { name: "Recall scores" }, { timeout: 3000 })).toBeInTheDocument();
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(2));
  });
});
