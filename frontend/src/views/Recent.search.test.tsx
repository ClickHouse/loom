import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
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
import { conflictKeys, listMemories, searchMemory } from "../api";

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
