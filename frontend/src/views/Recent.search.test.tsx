import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type { MemoryDetail, MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";
import { must } from "../test/must";

// While a search is active the Memories view asks memory.search with `explain: true` (so
// the per-hit evidence rides above the excerpt k) and puts a "Why this result" button in
// each ranked row's meta slot, whose popover holds the scoring. The count
// is the toolbar's alone: no summary line, no "Results" heading.

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    // The page list answers the feed; a `contains` list (the keyword half of a search)
    // answers nothing unless a test says otherwise.
    listMemories: vi.fn(async (_id: unknown, _limit?: number, opts?: { contains?: string }) =>
      opts?.contains
        ? { entries: [], total: 0, today: 0, truncated: false }
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    ),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    listConflicts: vi.fn(async () => ({ rows: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    searchMemory: vi.fn(async () => ({
      results: [
        {
          memory_key: "coffee_pref",
          memory_type: "semantic",
          content_excerpt: "prefers oat milk in coffee",
          distance: 0.3,
          similarity: 0.7,
          rank_score: 0.8,
          confidence: 0.9,
          explanation: "fact hit from namespace alice; similarity=0.70; rank_score=0.80; mode=hybrid",
          score_components: { rank_score: 0.8, semantic: 0.7, key_match_boost_effective: 0.1 },
        },
      ],
      embedding_model: "fake",
    })),
  };
});
import { bulkGetMemories, conflictKeys, listMemories, searchMemory } from "../api";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const admin: WhoAmI = {
  authenticated: true,
  subject: "a@example.com",
  org: "dev",
  roles: ["ADMIN"],
  namespaces: ["alice"],
};

describe("Recent search why-line", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("sends explain:true and renders the ranking terms under the ranked hit", async () => {
    render(<Recent />);
    await waitFor(() => expect(listMemories).toHaveBeenCalled());
    await screen.findByText("coffee_pref");

    await userEvent.type(screen.getByLabelText("Search memories"), "milk");

    await waitFor(() => expect(searchMemory).toHaveBeenCalled(), { timeout: 3000 });
    const [, query, opts] = must(vi.mocked(searchMemory).mock.calls.at(-1), "last searchMemory call");
    expect(query).toBe("milk");
    expect(opts).toMatchObject({ top_k: 50, explain: true, legs: ["memory"] }); // Memory legs only

    // The hit that did not match carries no button; the scoring stays out of the row.
    const button = await screen.findByRole("button", { name: "Why this result" });
    expect(screen.getAllByRole("button", { name: "Why this result" })).toHaveLength(1);
    expect(screen.queryByTestId("search-why")).toBeNull();
    expect(button.closest("[data-memory-key]")).toHaveAttribute("data-memory-key", "coffee_pref");
    await userEvent.click(button);
    const why = await screen.findByTestId("search-why");
    expect(why).toHaveTextContent("#1");
    expect(why).toHaveTextContent("similarity 0.70");
    expect(why).toHaveTextContent("semantic +0.70");
    expect(why).toHaveTextContent("key match +0.10");
    // One count, in the toolbar; no summary line or Results heading on screen.
    expect(screen.getByLabelText("Search and filters")).toHaveTextContent("1 match");
    expect(screen.queryByRole("heading", { name: /Results/ })).toBeNull();
  });
});

describe("Recent search under a tag filter", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("asks the keyword half for the rows that pass the tag filter too", async () => {
    vi.mocked(listMemories).mockImplementation(async () => ({
      entries: [entry("coffee_pref")],
      total: 1,
      today: 0,
      truncated: false,
    }));
    render(<Recent />);
    await screen.findByText("coffee_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:work coffee");
    await waitFor(
      () =>
        expect(
          vi.mocked(listMemories).mock.calls.some(([, , o]) => o?.contains === "coffee" && o.tags?.[0] === "work"),
        ).toBe(true),
      { timeout: 3000 },
    );
    // One ranked search for the query: the filter landing a moment later re-sends only the
    // keyword read, whose last call carries the tag.
    await new Promise((r) => setTimeout(r, 400));
    expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "coffee")).toHaveLength(1);
    const keyword = vi.mocked(listMemories).mock.calls.filter(([, , o]) => o?.contains === "coffee");
    expect(keyword.at(-1)?.[2]?.tags).toEqual(["work"]);
  });
});

describe("Recent search after a local write", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("keeps the ranked hits on screen while a write ranks again, then shows the hits as they now are", async () => {
    let written = false;
    let answerRanked: (v: unknown) => void = () => {};
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [entry("milk_note")], total: 1, today: 0, truncated: false }
        : { entries: [entry("milk_note"), entry("oat_pref"), entry("old_pref")], total: 3, today: 0, truncated: false },
    );
    const hit = (memory_key: string) => ({
      memory_key,
      memory_type: "semantic",
      content_excerpt: memory_key,
      distance: 0.3,
      similarity: 0.7,
      rank_score: 0.8,
      confidence: 0.9,
    });
    vi.mocked(searchMemory).mockImplementation(async () => {
      if (!written) return { results: [hit("oat_pref"), hit("old_pref")], embedding_model: "m" } as never;
      return new Promise((r) => (answerRanked = r)) as never;
    });
    render(<Recent />);
    await screen.findByText("old_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    await waitFor(() => expect(screen.getByLabelText("Search and filters")).toHaveTextContent("3 matches"), {
      timeout: 3000,
    });
    // A write here (old_pref deleted): the search runs again and its ranking is in flight.
    written = true;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk")).toHaveLength(2), {
      timeout: 3000,
    });
    expect(screen.getByText("oat_pref")).toBeInTheDocument();
    // Its scoring stays while the new ranking reads.
    expect(screen.getAllByRole("button", { name: "Why this result" }).length).toBeGreaterThan(0);
    await act(async () => answerRanked({ results: [hit("oat_pref")], embedding_model: "m" }));
    await waitFor(() => expect(screen.queryByText("old_pref")).toBeNull());
    expect(screen.getByText("oat_pref")).toBeInTheDocument();
  });

  it("while a write ranks again, keeps no hit read from outside the page before the write", async () => {
    let written = false;
    vi.mocked(listMemories).mockResolvedValue({ entries: [entry("milk_note")], total: 1, today: 0, truncated: false });
    onTestFinished(() => {
      vi.mocked(bulkGetMemories).mockImplementation(async () => ({ results: {}, missing: [] }));
    });
    vi.mocked(bulkGetMemories).mockImplementation(async (_id, keys) => ({
      results: Object.fromEntries(keys.map((k) => [k, { ...entry(k), value: k } as unknown as MemoryDetail])),
      missing: [],
    }));
    vi.mocked(searchMemory).mockImplementation(async () => {
      if (written) return new Promise(() => {}) as never;
      return {
        results: [
          {
            memory_key: "far_pref",
            memory_type: "semantic",
            content_excerpt: "far_pref",
            distance: 0.3,
            similarity: 0.7,
          },
        ],
        embedding_model: "m",
      } as never;
    });
    render(<Recent />);
    await screen.findByText("milk_note");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    expect((await screen.findAllByText("far_pref", {}, { timeout: 3000 })).length).toBeGreaterThan(0);
    // A write (far_pref may be gone): its copy from before the write is not drawn again.
    written = true;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk")).toHaveLength(2), {
      timeout: 3000,
    });
    await waitFor(() => expect(screen.queryAllByText("far_pref")).toHaveLength(0));
    expect(screen.getAllByText("milk_note").length).toBeGreaterThan(0);
  });

  it("a failed re-rank that kept nothing still forgets its ranking for the next run", async () => {
    let phase = 0;
    onTestFinished(() => {
      vi.mocked(bulkGetMemories).mockImplementation(async () => ({ results: {}, missing: [] }));
    });
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains || phase < 2
        ? { entries: [entry("milk_note")], total: 1, today: 0, truncated: false }
        : { entries: [entry("milk_note"), entry("far_pref")], total: 2, today: 0, truncated: false },
    );
    vi.mocked(bulkGetMemories).mockImplementation(async (_id, keys) => ({
      results: Object.fromEntries(keys.map((k) => [k, { ...entry(k), value: k } as unknown as MemoryDetail])),
      missing: [],
    }));
    vi.mocked(searchMemory).mockImplementation(async () => {
      if (phase === 1) throw new Error("search down");
      if (phase === 2) return new Promise(() => {}) as never;
      return {
        results: [
          {
            memory_key: "far_pref",
            memory_type: "semantic",
            content_excerpt: "far_pref",
            distance: 0.3,
            similarity: 0.7,
          },
        ],
        embedding_model: "m",
      } as never;
    });
    render(<Recent />);
    await screen.findByText("milk_note");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    expect((await screen.findAllByText("far_pref", {}, { timeout: 3000 })).length).toBeGreaterThan(0);
    const runs = () => vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk").length;
    // A write; the ranking fails, and far_pref (outside the page) is not kept.
    phase = 1;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(runs()).toBe(2), { timeout: 3000 });
    await waitFor(() => expect(screen.queryAllByText("far_pref")).toHaveLength(0));
    // far_pref joins the page; another write ranks again and is still reading.
    phase = 2;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(runs()).toBe(3), { timeout: 3000 });
    // The run's keyword read answers at once; let it draw before the negative check.
    await act(async () => {});
    expect(screen.queryAllByText("far_pref")).toHaveLength(0);
    expect(screen.getAllByText("milk_note").length).toBeGreaterThan(0);
  });

  it("says no memory matches the typed words, bare", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [], total: 0, today: 0, truncated: false }
        : { entries: [entry("milk_note")], total: 1, today: 0, truncated: false },
    );
    vi.mocked(searchMemory).mockResolvedValue({ results: [], embedding_model: "m" } as never);
    render(<Recent />);
    await screen.findByText("milk_note");
    await userEvent.type(screen.getByLabelText("Search memories"), "zebra");
    expect(
      await screen.findByText((_, el) => el?.textContent === "No memories match zebra", {}, { timeout: 3000 }),
    ).toBeInTheDocument();
  });

  it("a search that failed outright brings none of its earlier ranked hits back on the next run", async () => {
    let phase = 0;
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? phase === 1
          ? { entries: [], total: 0, today: 0, truncated: false }
          : { entries: [entry("milk_note")], total: 1, today: 0, truncated: false }
        : { entries: [entry("milk_note"), entry("oat_pref")], total: 2, today: 0, truncated: false },
    );
    const hit = (memory_key: string) => ({
      memory_key,
      memory_type: "semantic",
      content_excerpt: memory_key,
      distance: 0.3,
      similarity: 0.7,
      rank_score: 0.8,
      confidence: 0.9,
    });
    vi.mocked(searchMemory).mockImplementation(async () => {
      if (phase === 1) throw new Error("search down");
      if (phase === 2) return new Promise(() => {}) as never;
      return { results: [hit("oat_pref")], embedding_model: "m" } as never;
    });
    render(<Recent />);
    await screen.findByText("oat_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    await waitFor(() => expect(screen.getByLabelText("Search and filters")).toHaveTextContent("2 matches"), {
      timeout: 3000,
    });
    const runs = () => vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk").length;
    // A write; no keyword rows and the ranking fails: the search fails as a whole.
    phase = 1;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(runs()).toBe(2), { timeout: 3000 });
    await waitFor(() => expect(screen.queryByText("milk_note")).toBeNull());
    // Another write: keyword rows again, the ranking still reading.
    phase = 2;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(runs()).toBe(3), { timeout: 3000 });
    expect(await screen.findByText("milk_note")).toBeInTheDocument();
    expect(screen.queryByText("oat_pref")).toBeNull();
  });

  it("goes back to the keyword rows when the ranking after a write fails", async () => {
    let written = false;
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [entry("milk_note")], total: 1, today: 0, truncated: false }
        : { entries: [entry("milk_note"), entry("oat_pref")], total: 2, today: 0, truncated: false },
    );
    const hit = (memory_key: string) => ({
      memory_key,
      memory_type: "semantic",
      content_excerpt: memory_key,
      distance: 0.3,
      similarity: 0.7,
      rank_score: 0.8,
      confidence: 0.9,
      score_components: { semantic: 0.7 },
    });
    vi.mocked(searchMemory).mockImplementation(async () => {
      if (written) throw new Error("search down");
      return { results: [hit("oat_pref")], embedding_model: "m" } as never;
    });
    render(<Recent />);
    await screen.findByText("oat_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    await waitFor(() => expect(screen.getByLabelText("Search and filters")).toHaveTextContent("2 matches"), {
      timeout: 3000,
    });
    written = true;
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk")).toHaveLength(2), {
      timeout: 3000,
    });
    await waitFor(() => expect(screen.queryByText("oat_pref")).toBeNull());
    expect(screen.getByText("milk_note")).toBeInTheDocument();
  });

  it("searches again after a write here, so a deleted or pinned hit shows as it now is", async () => {
    vi.mocked(listMemories).mockImplementation(async () => ({
      entries: [entry("coffee_pref")],
      total: 1,
      today: 0,
      truncated: false,
    }));
    render(<Recent />);
    await screen.findByText("coffee_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    await waitFor(() => expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk")).toHaveLength(1), {
      timeout: 3000,
    });
    act(() => useStore.getState().bumpMemoriesVersion());
    await waitFor(() => expect(vi.mocked(searchMemory).mock.calls.filter(([, q]) => q === "milk")).toHaveLength(2), {
      timeout: 3000,
    });
  });
});

describe("Recent across a namespace switch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("drops the old namespace's search results while the new one's search reads", async () => {
    const pending = new Promise<never>(() => {});
    vi.mocked(listMemories).mockImplementation(async (id, _limit, opts) => {
      const bob = (id as { namespace: string }).namespace === "bob";
      if (opts?.contains)
        return bob ? pending : { entries: [entry("alice_only")], total: 1, today: 0, truncated: false };
      return {
        entries: bob ? [entry("bob_row")] : [entry("coffee_pref"), entry("tea_pref")],
        total: 2,
        today: 0,
        truncated: false,
      };
    });
    vi.mocked(searchMemory).mockImplementation(async (id) => {
      if ((id as { namespace: string }).namespace === "bob") return pending;
      return { results: [], embedding_model: "fake" } as never;
    });
    render(<Recent />);
    await screen.findByText("coffee_pref");
    await userEvent.type(screen.getByLabelText("Search memories"), "milk");
    expect(await screen.findByText("alice_only", {}, { timeout: 3000 })).toBeInTheDocument();
    act(() =>
      useStore.setState({
        identity: { org: "dev", namespace: "bob", agent: "loom-ui" },
        myNamespaces: [row("bob", "admin")],
      }),
    );
    await waitFor(() => expect(screen.queryByText("alice_only")).toBeNull());
  });

  it("clearing the box while a search reads gives the count back", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? new Promise<never>(() => {})
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("coffee_pref");
    const box = screen.getByLabelText("Search memories");
    await userEvent.type(box, "milk");
    await waitFor(
      () => expect(vi.mocked(listMemories).mock.calls.some(([, , o]) => o?.contains === "milk")).toBe(true),
      { timeout: 3000 },
    );
    await userEvent.clear(box);
    const bar = screen.getByLabelText("Search and filters");
    await waitFor(() => expect(bar.querySelector("[aria-busy]")).toBeNull());
    expect(bar).toHaveTextContent("1–2 of 2");
  });

  it("shows no conflict chip from the old namespace while the new one's conflicts read", async () => {
    // Both namespaces hold the same keys (the mock's implementation outlives each test).
    vi.mocked(listMemories).mockImplementation(async () => ({
      entries: [entry("coffee_pref"), entry("tea_pref")],
      total: 2,
      today: 0,
      truncated: false,
    }));
    vi.mocked(conflictKeys).mockImplementation(async (id) =>
      (id as { namespace: string }).namespace === "bob" ? new Promise<string[]>(() => {}) : ["tea_pref"],
    );
    render(<Recent />);
    await screen.findByText("tea_pref");
    await waitFor(() => expect(screen.getAllByText("Conflicted").length).toBeGreaterThan(0));
    // bob holds a row of the same key; its conflict read has not answered.
    act(() =>
      useStore.setState({
        identity: { org: "dev", namespace: "bob", agent: "loom-ui" },
        myNamespaces: [row("bob", "admin")],
      }),
    );
    await screen.findByText("tea_pref");
    expect(screen.queryByText("Conflicted")).toBeNull();
  });
});

describe("Recent search asks for what it can show", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("one character is not a query; two are", async () => {
    render(<Recent />);
    await waitFor(() => expect(listMemories).toHaveBeenCalled());
    const box = screen.getByLabelText("Search memories");
    fireEvent.change(box, { target: { value: "m" } });
    await new Promise((r) => setTimeout(r, 350));
    expect(searchMemory).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: "mi" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(1), { timeout: 3000 });
  });

  it("a retype before the answer aborts the stale search", async () => {
    let release: () => void = () => {};
    const signals: AbortSignal[] = [];
    vi.mocked(searchMemory).mockImplementation(async (_id, _q, opts) => {
      signals.push(must(opts?.signal, "search signal"));
      await new Promise<void>((r) => {
        release = r;
      });
      return { results: [], embedding_model: "fake" };
    });
    render(<Recent />);
    await waitFor(() => expect(listMemories).toHaveBeenCalled());
    const box = screen.getByLabelText("Search memories");
    fireEvent.change(box, { target: { value: "milk" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(1), { timeout: 3000 });
    expect(signals[0].aborted).toBe(false);
    fireEvent.change(box, { target: { value: "milk oat" } });
    await waitFor(() => expect(searchMemory).toHaveBeenCalledTimes(2), { timeout: 3000 });
    expect(signals[0].aborted).toBe(true); // the first search is cancelled…
    expect(signals[1].aborted).toBe(false); // …the current one is not
    release();
  });
});

describe("Recent search finds every row that names the word", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row("alice", "admin")],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
    // An earlier describe leaves searchMemory on a never-resolving implementation.
    vi.mocked(searchMemory).mockImplementation(async () => ({
      results: [
        {
          memory_key: "coffee_pref",
          memory_type: "semantic",
          content_excerpt: "prefers oat milk in coffee",
          distance: 0.3,
          similarity: 0.7,
          rank_score: 0.8,
          confidence: 0.9,
          explanation: "fact hit; similarity=0.70",
          score_components: { rank_score: 0.8, semantic: 0.7 },
        },
      ],
      embedding_model: "fake",
    }));
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("puts the rows holding the text first, newest first, even outside the loaded page", async () => {
    // `azure_new` is in neither the loaded page nor the ranked top k — the row a ranked
    // search alone would never show.
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [entry("azure_new"), entry("coffee_pref")], total: 146, today: 1, truncated: false }
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("coffee_pref");

    await userEvent.type(screen.getByLabelText("Search memories"), "Azure");

    await screen.findByText("azure_new");
    const kw = must(
      vi.mocked(listMemories).mock.calls.find((c) => c[2]?.contains),
      "keyword list call",
    );
    expect(kw[2]).toMatchObject({ contains: "Azure" });
    expect(await screen.findAllByRole("button", { name: "Why this result" })).toHaveLength(2);
    await userEvent.click(screen.getAllByRole("button", { name: "Why this result" })[0]);
    expect(await screen.findByTestId("search-why")).toHaveTextContent('Contains "Azure"');
    // The toolbar's count says the keyword rows are the newest of more.
    expect(screen.getByText(/· the newest 2 of 146 holding "Azure"/).closest("[aria-live]")).not.toBeNull();
    // The keyword rows lead: the newest match is the first card.
    const keys = screen.getAllByText(/^(azure_new|coffee_pref|tea_pref)$/).map((n) => n.textContent);
    expect(keys[0]).toBe("azure_new");
  });

  it("paints the keyword rows before the ranked search answers", async () => {
    let answer: () => void = () => {};
    vi.mocked(searchMemory).mockImplementation(
      () =>
        new Promise((resolve) => {
          answer = () =>
            resolve({
              results: [
                {
                  memory_key: "tea_pref",
                  memory_type: "semantic",
                  content_excerpt: "green tea",
                  distance: 0.3,
                  similarity: 0.7,
                  rank_score: 0.8,
                  confidence: 0.9,
                  explanation: "fact hit; similarity=0.70",
                  score_components: { rank_score: 0.8, semantic: 0.7 },
                },
              ],
              embedding_model: "fake",
            });
        }),
    );
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [entry("azure_new")], total: 1, today: 1, truncated: false }
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("coffee_pref");

    await userEvent.type(screen.getByLabelText("Search memories"), "Azure");

    // The ranked search is still pending: the keyword row is already on screen.
    await screen.findByText("azure_new");
    expect(screen.getByText(/finding related/)).toBeInTheDocument();
    expect(screen.queryByText("tea_pref")).not.toBeInTheDocument();

    answer();
    await screen.findByText("tea_pref");
    await waitFor(() => expect(screen.queryByText(/finding related/)).not.toBeInTheDocument());
    const keys = screen.getAllByText(/^(azure_new|tea_pref)$/).map((n) => n.textContent);
    expect(keys).toEqual(["azure_new", "tea_pref"]);
  });

  it("a failed ranked search keeps the keyword rows", async () => {
    vi.mocked(searchMemory).mockRejectedValue(new Error("search down"));
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.contains
        ? { entries: [entry("azure_new")], total: 1, today: 1, truncated: false }
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("coffee_pref");

    await userEvent.type(screen.getByLabelText("Search memories"), "Azure");

    await screen.findByText("azure_new");
    await waitFor(() => expect(screen.queryByText(/finding related/)).not.toBeInTheDocument());
    expect(screen.getByText("azure_new")).toBeInTheDocument();
  });

  it("a failed keyword read leaves the ranked hits standing", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) => {
      if (opts?.contains) throw new Error("boom");
      return { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false };
    });
    render(<Recent />);
    await screen.findByText("coffee_pref");

    await userEvent.type(screen.getByLabelText("Search memories"), "milk");

    await userEvent.click(await screen.findByRole("button", { name: "Why this result" }));
    expect(await screen.findByTestId("search-why")).toHaveTextContent("similarity 0.70");
    // No row holds the word: the count is the ranked matches alone.
    expect(screen.getByText(/^\d+ match(es)?$/).closest("[aria-live]")).not.toBeNull();
  });
});
