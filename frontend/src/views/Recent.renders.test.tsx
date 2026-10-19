import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// The cards are memoised: a keystroke in the search box renders the page, not every card
// under it. A card renders its AgentBadge each time it renders, so the badge counts them.

const renders = { count: 0 };
vi.mock("../components/AgentBadge", () => ({
  AgentBadge: ({ agent }: { agent: string }) => {
    renders.count += 1;
    return <span>{agent}</span>;
  },
}));

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: ["alerts"],
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    // The keyword half holds tea_pref; the ranked half ranks coffee_pref.
    listMemories: vi.fn(async (_id: unknown, _limit?: number, opts?: { contains?: string }) =>
      opts?.contains
        ? { entries: [entry("tea_pref")], total: 1, today: 1, truncated: false }
        : { entries: [entry("coffee_pref"), entry("tea_pref")], total: 2, today: 2, truncated: false },
    ),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
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
          explanation: "similarity=0.70",
          score_components: { rank_score: 0.8, semantic: 0.7 },
        },
      ],
      embedding_model: "fake",
    })),
  };
});

const row: MyNamespaceEntry = {
  namespace: "alice",
  role: "admin",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};
const admin: WhoAmI = { authenticated: true, subject: "a@example.com", org: "dev", roles: ["ADMIN"], namespaces: [] };

describe("Recent card renders", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    renders.count = 0;
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [row],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("a keystroke while results show renders no card again", async () => {
    render(<Recent />);
    await screen.findByText("coffee_pref");
    const box = screen.getByLabelText("Search memories");
    fireEvent.change(box, { target: { value: "milk" } });
    // Both rows show a "Why this result": the keyword row and the ranked one.
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Why this result" })).toHaveLength(2), {
      timeout: 3000,
    });
    renders.count = 0;
    // The next search waits out its debounce; until then the results on screen stand.
    act(() => {
      fireEvent.change(box, { target: { value: "milky" } });
    });
    expect(renders.count).toBe(0);
  });

  it("typing an agent filter a key at a time renders no card again", async () => {
    render(<Recent />);
    await screen.findByText("coffee_pref");
    const box = screen.getByLabelText("Search memories");
    fireEvent.change(box, { target: { value: "milk" } });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Why this result" })).toHaveLength(2), {
      timeout: 3000,
    });
    renders.count = 0;
    act(() => {
      fireEvent.change(box, { target: { value: "milk agent:c" } });
    });
    act(() => {
      fireEvent.change(box, { target: { value: "milk agent:cl" } });
    });
    expect(renders.count).toBe(0);
  });
});
