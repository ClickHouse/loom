import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, SearchHit, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { listMemories, searchMemory } from "../api";
import { RecallLab } from "./RecallLab";
import { Recent } from "./Recent";

// Recall Lab explains the list the Memories page shows for a query: the same rows in the
// same order — keyword matches newest first, then the ranked hits they do not hold.

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

const hit = (memory_key: string, rank_score: number): SearchHit => ({
  memory_key,
  memory_type: "semantic",
  content_excerpt: `value of ${memory_key}`,
  distance: 0.4,
  similarity: 0.6,
  rank_score,
  confidence: 1,
  score_components: { semantic: 0.6 },
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async (_id: unknown, _limit?: number, opts?: { contains?: string }) =>
      opts?.contains
        ? { entries: [entry("newer_azure"), entry("older_azure")], total: 2, today: 0, truncated: false }
        : {
            entries: [entry("newer_azure"), entry("older_azure"), entry("blob_storage")],
            total: 3,
            today: 0,
            truncated: false,
          },
    ),
    searchMemory: vi.fn(async () => ({
      results: [hit("older_azure", 0.9), hit("blob_storage", 0.7)],
      embedding_model: "m",
    })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    listConflicts: vi.fn(async () => ({ rows: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    listSearchTraceLog: vi.fn(async () => ({ rows: [] })),
    isAuthDisabled: () => false,
  };
});

const admin: WhoAmI = {
  authenticated: true,
  subject: "a@example.com",
  org: "dev",
  roles: ["ADMIN"],
  namespaces: ["alice"],
};

const shownKeys = () => screen.getAllByText(/^(newer_azure|older_azure|blob_storage)$/).map((el) => el.textContent);

describe("Recall Lab and the Memories list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: [
        {
          namespace: "alice",
          role: "admin",
          status: "active",
          visibility: "private",
          description: "",
          joined_at: null,
          is_new: false,
        },
      ],
      identity: { org: "dev", namespace: "alice", agent: "loom-ui" },
      recallRunFilter: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("show the same rows in the same order for one query", async () => {
    useStore.setState({ view: "memories" });
    render(<Recent />);
    await screen.findByText("blob_storage");
    await userEvent.type(screen.getByLabelText("Search memories"), "Azure");
    await waitFor(() => expect(shownKeys()).toEqual(["newer_azure", "older_azure", "blob_storage"]), {
      timeout: 3000,
    });
    const memories = shownKeys();
    cleanup();

    useStore.setState({ view: "recall" });
    render(<RecallLab />);
    await userEvent.type(screen.getByLabelText("Recall query"), "Azure");
    await waitFor(() => expect(shownKeys()).toEqual(memories), { timeout: 3000 });
    // The keyword rows say why they lead; the ranked one says what ranked it.
    expect(screen.getAllByText("keyword match")).toHaveLength(2);
    expect(screen.getAllByText("semantic +0.60")).toHaveLength(2);
  });

  it("keep the keyword rows when the ranked search fails", async () => {
    vi.mocked(searchMemory).mockRejectedValueOnce(new Error("search down"));
    useStore.setState({ view: "recall" });
    render(<RecallLab />);
    await userEvent.type(screen.getByLabelText("Recall query"), "Azure");
    await waitFor(() => expect(shownKeys()).toEqual(["newer_azure", "older_azure"]), { timeout: 3000 });
    expect(screen.getByText(/Ranked search failed .*; showing the keyword matches only\./)).toBeInTheDocument();
  });

  it("send the text the Memories list sends: whitespace collapsed, tag: and agent: left out", async () => {
    useStore.setState({ view: "recall" });
    render(<RecallLab />);
    fireEvent.change(screen.getByLabelText("Recall query"), {
      target: { value: "  tag:prod  Azure\tblob  agent:ci " },
    });
    await waitFor(() => expect(searchMemory).toHaveBeenCalled(), { timeout: 3000 });
    expect(vi.mocked(searchMemory).mock.calls.at(-1)?.[1]).toBe("Azure blob");
    const contains = vi
      .mocked(listMemories)
      .mock.calls.map((c) => (c[2] as { contains?: string } | undefined)?.contains);
    expect(contains).toContain("Azure blob");
  });
});
