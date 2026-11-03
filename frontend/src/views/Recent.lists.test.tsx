import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MemoryListEntry, MemoryListResponse, MyNamespaceEntry, WhoAmI } from "../api";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// The Memories list against the list rules: one count, in the toolbar; the toolbar in
// every state, with the one state pattern under it; an Agent and a Status select in
// place of the chip row and the header toggle; the select-all and Shift-click range
// selection, cleared by Escape.

const entry = (memory_key: string, over: Partial<MemoryListEntry> = {}): MemoryListEntry => ({
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
  ...over,
});

const ROWS = [
  entry("a/pinned", { pinned: true }),
  entry("b/verified", { verification: "human", verified_by: "human:o@example.com", agent: "support-bot" }),
  entry("c/conflicted", { agent: "support-bot" }),
  entry("d/plain"),
];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(),
    getMemory: vi.fn(),
    pinMemory: vi.fn(async () => ({ version: 2, pinned: true })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => ["c/conflicted"]),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { getMemory, listMemories, pinMemory } from "../api";

const owner: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team"],
};
const row: MyNamespaceEntry = {
  namespace: "team",
  role: "admin",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

// Click UI's Select items carry no option role; they are addressed by value.
const item = (value: string) => document.querySelector<HTMLElement>(`[cui-select-item][data-value="${value}"]`);
const toolbar = () => screen.getByLabelText("Search and filters");
const shown = () => ROWS.map((r) => r.memory_key).filter((k) => screen.queryByText(k));

async function pick(select: string, value: string) {
  await userEvent.click(within(toolbar()).getByRole("button", { name: select }));
  await waitFor(() => expect(item(value)).not.toBeNull());
  await userEvent.click(item(value) as HTMLElement);
}

describe("Memories list chrome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetNamespaceRoleCache();
    vi.mocked(listMemories).mockResolvedValue({ entries: ROWS, total: 1234, today: 2, truncated: false });
    useStore.setState({
      authed: true,
      whoami: owner,
      myNamespaces: [row],
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      view: "memories",
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  // A browsed page, then reads held open: a new page size, then a filter cleared.
  function browseThenHold() {
    let hold = false;
    vi.mocked(listMemories).mockImplementation(async (_id, limit = 50, opts): Promise<MemoryListResponse> => {
      if (limit === 1000) return { entries: ROWS.slice(0, 2), total: 2, today: 0, truncated: false };
      const from = opts?.offset ?? 0;
      const answer = {
        entries: Array.from({ length: limit }, (_, i) => entry(`k${from + i}`)),
        total: 1234,
        today: 0,
        truncated: false,
      };
      return hold ? new Promise<MemoryListResponse>(() => {}) : answer;
    });
    return () => {
      hold = true;
    };
  }

  it("says it is loading, with the namespace's total, while a new page size reads", async () => {
    const holdReads = browseThenHold();
    render(<Recent />);
    await screen.findByText("k0");
    const pager = () => screen.getByRole("navigation", { name: "Memories pages" });
    holdReads();
    await userEvent.click(within(pager()).getByRole("button", { name: "Rows per page" }));
    await userEvent.click(await screen.findByText(/^100/));
    await waitFor(() => expect(pager()).toHaveTextContent("Loading… · 1,234 rows in all"));
  });

  it("does not count a cleared filter's matches as the namespace's total", async () => {
    const holdReads = browseThenHold();
    render(<Recent />);
    await screen.findByText("k0");
    await pick("Status", "pinned");
    await waitFor(() => expect(listMemories).toHaveBeenLastCalledWith(expect.anything(), 1000, expect.anything()));
    holdReads();
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Clear filters" }));
    const pager = await screen.findByRole("navigation", { name: "Memories pages" });
    expect(pager).toHaveTextContent("1,234 rows in all");
    expect(pager).not.toHaveTextContent(/\b2 rows in all/);
  });

  // A select narrows the list too: the row does not blame the typed tag alone.
  it("says the filters in force match nothing when a status narrows a typed tag", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.tags?.length
        ? { entries: [], total: 0, today: 0, truncated: false }
        : { entries: ROWS, total: 4, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:nomatch");
    expect(await screen.findByText((_, el) => el?.textContent === "No memories match tag:nomatch")).toBeInTheDocument();
    await pick("Status", "pinned");
    expect(
      await screen.findByText((_, el) => el?.textContent === "No memories match the filters in force"),
    ).toBeInTheDocument();
  });

  it("offers no pager over the old rows while a new filter's read is in flight", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, limit = 50): Promise<MemoryListResponse> => {
      if (limit === 1000) return new Promise<MemoryListResponse>(() => {});
      const entries = Array.from({ length: 50 }, (_, i) => entry(`p${i}`, { pinned: true }));
      return { entries, total: 1234, today: 0, truncated: false };
    });
    render(<Recent />);
    await screen.findByText("p0");
    expect(screen.getByRole("navigation", { name: "Memories pages" })).toHaveTextContent("of 1,234");
    await pick("Status", "pinned");
    await waitFor(() => expect(listMemories).toHaveBeenLastCalledWith(expect.anything(), 1000, expect.anything()));
    // The pinned rows on screen came from the browsed page: no "of 50" from them.
    expect(screen.queryByRole("navigation", { name: "Memories pages" })).toBeNull();
  });

  it("drops what the pager said of a page when a filter replaces its read", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, limit = 50, opts): Promise<MemoryListResponse> => {
      if (limit === 1000) {
        const entries = Array.from({ length: 30 }, (_, i) => entry(`pin${i}`, { pinned: true }));
        return { entries, total: 30, today: 0, truncated: false };
      }
      if ((opts?.offset ?? 0) > 0) return new Promise<MemoryListResponse>(() => {});
      return { entries: Array.from({ length: 50 }, (_, i) => entry(`k${i}`)), total: 1234, today: 0, truncated: false };
    });
    render(<Recent />);
    await screen.findByText("k0");
    const pager = () => screen.getByRole("navigation", { name: "Memories pages" });
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    expect(within(pager()).getByRole("status")).toHaveTextContent("Page 2 of 25");
    await pick("Status", "pinned");
    expect(await screen.findByText("pin0")).toBeInTheDocument();
    expect(within(pager()).getByRole("status")).toBeEmptyDOMElement();
  });

  it("counts a cut-short tag filter's matches from the server", async () => {
    // A viewer's read is cut to 100 rows; the server still counts every match.
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              entries: Array.from({ length: 100 }, (_, i) => entry(`t${i}`, { tags: ["api"] })),
              total: 340,
              today: 0,
              truncated: true,
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:api");
    await waitFor(() => expect(toolbar()).toHaveTextContent("340 matches"));
    expect(screen.getByText(/Showing the newest matches/)).toBeInTheDocument();
    // The pager covers the rows read: the newest 100, not every match.
    expect(screen.getByRole("navigation", { name: "Memories pages" })).toHaveTextContent("1–50 of the newest 100");
  });

  it("drops the server's count while a typed tag waits to be applied", async () => {
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              entries: Array.from({ length: 100 }, (_, i) =>
                entry(`t${i}`, { tags: i < 3 ? ["api", "web"] : ["api"] }),
              ),
              total: 340,
              today: 0,
              truncated: true,
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    const box = screen.getByLabelText("Search memories");
    await userEvent.type(box, "tag:api");
    await waitFor(() => expect(toolbar()).toHaveTextContent("340 matches"));
    // The second tag narrows the rows at once; its read waits for the typing to pause.
    fireEvent.change(box, { target: { value: "tag:api tag:web" } });
    expect(toolbar()).not.toHaveTextContent("340");
  });

  it("counts a cut-short status filter's pager plainly, the note saying what it covers", async () => {
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              entries: Array.from({ length: 1000 }, (_, i) => entry(`p${i}`, { pinned: i < 60 })),
              total: 5000,
              today: 0,
              truncated: true,
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await pick("Status", "pinned");
    await waitFor(() => expect(toolbar()).toHaveTextContent("60 matches"));
    // 60 pinned among the newest rows read: not "the newest 60".
    const pager = screen.getByRole("navigation", { name: "Memories pages" });
    expect(pager).toHaveTextContent("1–50 of 60");
    expect(pager).not.toHaveTextContent("newest");
    expect(screen.getByText(/among the newest ones read/)).toBeInTheDocument();
  });

  it("counts a cut-short tag read plainly once a status narrows it here too", async () => {
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              entries: Array.from({ length: 100 }, (_, i) => entry(`t${i}`, { tags: ["api"], pinned: i < 60 })),
              total: 340,
              today: 0,
              truncated: true,
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:api");
    await waitFor(() => expect(toolbar()).toHaveTextContent("340 matches"));
    await pick("Status", "pinned");
    await waitFor(() => expect(toolbar()).toHaveTextContent("60 matches"));
    expect(screen.getByRole("navigation", { name: "Memories pages" })).not.toHaveTextContent("newest");
  });

  it("counts the rows on screen when the read was not cut short, whatever the server total", async () => {
    // The page keeps profile rows out; the server counted them.
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              entries: [entry("a1", { tags: ["api"] }), entry("a2", { tags: ["api"] })],
              total: 300,
              today: 0,
              truncated: false,
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:api");
    await waitFor(() => expect(toolbar()).toHaveTextContent("2 matches"));
    expect(toolbar()).not.toHaveTextContent("300");
  });

  it("counts the rows on screen when the server matched the tags another way (a release behind)", async () => {
    vi.mocked(listMemories).mockImplementation(
      async (_id, limit = 50): Promise<MemoryListResponse> =>
        limit === 1000
          ? {
              // Any one tag: 300 rows, of which 2 carry both.
              entries: [
                entry("both1", { tags: ["api", "web"] }),
                entry("both2", { tags: ["api", "web"] }),
                entry("one", { tags: ["api"] }),
              ],
              total: 300,
              today: 0,
              truncated: true,
              ignored_args: ["tags_every"],
            }
          : { entries: ROWS, total: 1234, today: 0, truncated: false },
    );
    render(<Recent />);
    await screen.findByText("d/plain");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:api tag:web");
    await waitFor(() => expect(toolbar()).toHaveTextContent("2 matches"));
    expect(toolbar()).not.toHaveTextContent("300");
  });

  // Next waits for the page's read, and the pager pages from the rows on screen.
  it("asks for the next page once when Next is pressed twice while it loads", async () => {
    const offsets: number[] = [];
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) => {
      const from = opts?.offset ?? 0;
      offsets.push(from);
      const answer = {
        entries: Array.from({ length: 50 }, (_, i) => entry(`k${from + i}`)),
        total: 200,
        today: 0,
        truncated: false,
      };
      return from === 0 ? answer : new Promise<typeof answer>(() => {});
    });
    render(<Recent />);
    await screen.findByText("k0");
    const pager = () => screen.getByRole("navigation", { name: "Memories pages" });
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(offsets).toContain(50));
    // The rows on screen are still page 1, and the pager says so.
    expect(pager()).toHaveTextContent("1–50 of 200");
    expect(within(pager()).getByRole("button", { name: "Next page" })).toBeDisabled();
    fireEvent.click(within(pager()).getByRole("button", { name: "Next page" }));
    expect(offsets.filter((o) => o > 50)).toEqual([]);
  });

  it("says where the list stands once, at the end of the toolbar", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    expect(toolbar()).toHaveTextContent("1–4 of 1,234");
    // No "1,234 total" / "2 today" badges beside it.
    expect(screen.queryByText(/total$/)).toBeNull();
    expect(screen.queryByText(/today$/)).toBeNull();
  });

  it("keeps the toolbar up while the first page loads", async () => {
    vi.mocked(listMemories).mockImplementation(() => new Promise(() => {}));
    render(<Recent />);
    expect(screen.getByLabelText("Search memories")).toBeInTheDocument();
    expect(within(toolbar()).getByRole("button", { name: "Status" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading memories" })).toBeInTheDocument();
  });

  it("says a failed read with a Retry, never the empty state or a zero count", async () => {
    vi.mocked(listMemories).mockRejectedValueOnce(
      new ApiError({ status: 503, code: "INTERNAL", message: "ClickHouse timed out after 30 s", requestId: "req-42" }),
    );
    render(<Recent />);
    expect(await screen.findByText("Couldn't load memories")).toBeInTheDocument();
    expect(screen.getByText("req-42").tagName).toBe("CODE");
    expect(screen.queryByText("No memories yet")).toBeNull();
    expect(toolbar()).not.toHaveTextContent(/\d/);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("d/plain")).toBeInTheDocument();
  });

  it("shows one Add memory while the namespace is empty: the empty state's", async () => {
    vi.mocked(listMemories).mockResolvedValue({ entries: [], total: 0, today: 0, truncated: false });
    render(<Recent />);
    expect(await screen.findByText("No memories yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Add memory/ })).toHaveLength(1);
    expect(screen.getByLabelText("Search memories")).toBeInTheDocument();
  });

  it("filters by status: pinned, conflicted, verified", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    // No chip row under the toolbar.
    expect(screen.queryByRole("button", { name: /^Pinned/ })).toBeNull();
    await pick("Status", "pinned");
    await waitFor(() => expect(shown()).toEqual(["a/pinned"]));
    expect(toolbar()).toHaveTextContent("1 match");
    await pick("Status", "conflicted");
    await waitFor(() => expect(shown()).toEqual(["c/conflicted"]));
    await pick("Status", "verified");
    await waitFor(() => expect(shown()).toEqual(["b/verified"]));
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(shown()).toEqual(ROWS.map((r) => r.memory_key)));
  });

  it("filters by agent, and keeps every agent it has seen in the select", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    expect(within(toolbar()).getByRole("button", { name: "Agent" })).toHaveTextContent("Every agent");
    vi.mocked(listMemories).mockResolvedValue({
      entries: ROWS.filter((r) => r.agent === "support-bot"),
      total: 2,
      today: 0,
      truncated: false,
    });
    await pick("Agent", "support-bot");
    await waitFor(() => expect(shown()).toEqual(["b/verified", "c/conflicted"]));
    expect(vi.mocked(listMemories).mock.calls.at(-1)?.[2]).toMatchObject({ agent: "support-bot" });
    await userEvent.click(within(toolbar()).getByRole("button", { name: "Agent" }));
    await waitFor(() => expect(item("claude-desktop")).not.toBeNull());
  });

  it("selects a Shift-click range, every row on the page, and clears on Escape", async () => {
    render(<Recent />);
    await screen.findByText("d/plain");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "Select a/pinned" }));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("checkbox", { name: "Select c/conflicted" }));
    await user.keyboard("{/Shift}");
    expect(toolbar()).toHaveTextContent("3 selected");
    const all = within(toolbar()).getByRole("checkbox", { name: "Select all 4 on this page" });
    expect(all).toHaveAttribute("aria-checked", "mixed");
    await user.click(all);
    expect(toolbar()).toHaveTextContent("4 selected");
    expect(within(toolbar()).getByRole("checkbox", { name: "Select all 4 on this page" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Escape}");
    expect(toolbar()).not.toHaveTextContent("selected");
  });

  // memory.pin writes a whole new version: the bulk Pin hands over the row it read.
  it("pins a selected row with the row memory.get read, tags and provenance included", async () => {
    const detail: MemoryDetail = {
      found: true,
      value: "value of d/plain",
      memory_type: "semantic",
      confidence: 0.8,
      version: 1,
      pinned: false,
      created_at: "2026-09-18T09:00:00Z",
      tags: ["infra"],
      source_trace: "conversation:t1",
      agent: "claude-desktop",
    };
    vi.mocked(getMemory).mockResolvedValue(detail);
    render(<Recent />);
    await screen.findByText("d/plain");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "Select d/plain" }));
    await user.click(within(toolbar()).getByRole("button", { name: "Pin" }));
    await waitFor(() => expect(pinMemory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(pinMemory).mock.calls[0]).toEqual([
      { org: "dev", namespace: "team", agent: "claude-desktop" },
      "d/plain",
      detail,
    ]);
  });
});
