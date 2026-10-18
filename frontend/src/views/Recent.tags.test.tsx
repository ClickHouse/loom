import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// A tag on a memory card adds that tag to the filter, and another tag narrows it further
// (every tag must match, as `tag:a tag:b` typed in the search does). The tags in force
// are the `tag:` tokens in the search field (no chip beside it), so they
// stay in view as the list scrolls.

const entry = (memory_key: string, tags: string[]): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags,
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({
      entries: [
        entry("both", ["Alerts", "storage"]),
        entry("alerts_only", ["Alerts"]),
        entry("storage_only", ["storage"]),
      ],
      total: 3,
      today: 3,
    })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    listConflicts: vi.fn(async () => ({ rows: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { listMemories } from "../api";

const owner: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team-alerts"],
};
const row: MyNamespaceEntry = {
  namespace: "team-alerts",
  role: "admin",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

const shown = () => ["both", "alerts_only", "storage_only"].filter((k) => screen.queryByText(k));
// Click UI's Select items carry no option role; they are addressed by value.
const item = (value: string) => document.querySelector<HTMLElement>(`[cui-select-item][data-value="${value}"]`);
const card = (key: string) => screen.getByText(key).closest("[data-memory-key]") as HTMLElement;

describe("Memories: filtering by tags", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: owner,
      myNamespaces: [row],
      identity: { org: "dev", namespace: "team-alerts", agent: "loom-ui" },
      view: "memories",
      inspectedMemoryKey: null,
      memoryTagFilter: null,
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, memoryTagFilter: null });
  });

  async function openMemories() {
    render(<Recent />);
    await waitFor(() => expect(listMemories).toHaveBeenCalled());
    await screen.findByText("both");
  }

  it("asks for the next page once when Next is pressed twice while it loads", async () => {
    const offsets: number[] = [];
    // The other cases read the module's default answer: put it back afterwards.
    const original = vi.mocked(listMemories).getMockImplementation();
    onTestFinished(() => {
      if (original) vi.mocked(listMemories).mockImplementation(original);
    });
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) => {
      const from = opts?.offset ?? 0;
      offsets.push(from);
      const entries = Array.from({ length: 50 }, (_, i) => entry(`k${from + i}`, []));
      const answer = { entries, total: 200, today: 0, truncated: false };
      return from === 0 ? answer : new Promise<typeof answer>(() => {});
    });
    render(<Recent />);
    await screen.findByText("k0");
    const next = () =>
      within(screen.getByRole("navigation", { name: "Memories pages" })).getByRole("button", { name: "Next page" });
    fireEvent.click(next());
    await waitFor(() => expect(offsets).toContain(50));
    expect(next()).toBeDisabled();
    fireEvent.click(next());
    expect(offsets.filter((o) => o > 50)).toEqual([]);
  });

  it("stacks a second tag onto the first, and shows each in the search field", async () => {
    await openMemories();
    await userEvent.click(within(card("alerts_only")).getByRole("button", { name: "Filter by tag Alerts" }));
    expect(shown()).toEqual(["both", "alerts_only"]);
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag storage" }));
    expect(shown()).toEqual(["both"]);

    const toolbar = screen.getByLabelText("Search and filters");
    expect(screen.getByLabelText("Search memories")).toHaveValue("tag:Alerts tag:storage");
    // The field says it once; no chip repeats it.
    expect(within(toolbar).queryByRole("group", { name: "Filters in force" })).toBeNull();
    expect(within(toolbar).queryByText("tag:Alerts")).toBeNull();
    // On the card, an applied tag reads as pressed.
    expect(within(card("both")).getByRole("button", { name: "Filter by tag storage" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("takes one tag out from the card, and clears them all", async () => {
    await openMemories();
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag Alerts" }));
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag storage" }));
    const toolbar = screen.getByLabelText("Search and filters");
    expect(shown()).toEqual(["both"]);
    // Pressing an applied tag on a card takes it out again.
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag storage" }));
    expect(shown()).toEqual(["both", "alerts_only"]);
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag Alerts" }));
    expect(shown()).toEqual(["both", "alerts_only", "storage_only"]);

    await userEvent.type(screen.getByLabelText("Search memories"), "tag:alerts tag:storage");
    await userEvent.click(within(toolbar).getAllByRole("button", { name: "Clear filters" })[0]);
    expect(shown()).toEqual(["both", "alerts_only", "storage_only"]);
    expect(screen.getByLabelText("Search memories")).toHaveValue("");
  });

  it("a tag picked elsewhere in the app joins the same filter", async () => {
    useStore.setState({ memoryTagFilter: "storage" });
    await openMemories();
    expect(shown()).toEqual(["both", "storage_only"]);
    expect(screen.getByLabelText("Search memories")).toHaveValue("tag:storage");
  });

  // The namespace holds more than a page: the filter is asked of the server, so a match
  // past the first page shows, and there is no pager over rows the filter left out.
  it("asks the server for the tagged rows of one agent, not the current page", async () => {
    await openMemories();
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.tags?.length
        ? {
            entries: [{ ...entry("far_away", ["kind:artifact"]), agent: "claude-desktop" }],
            total: 1,
            today: 0,
            truncated: false,
          }
        : { entries: [entry("both", ["Alerts", "storage"])], total: 250, today: 0, truncated: false },
    );
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:kind:artifact agent:claude-desktop");
    expect(await screen.findByText("far_away")).toBeInTheDocument();
    const [, limit, opts] = vi.mocked(listMemories).mock.calls.at(-1)!;
    expect(opts).toMatchObject({ tags: ["kind:artifact"], agent: "claude-desktop", offset: 0 });
    expect(limit).toBe(1000);
    expect(screen.queryByRole("navigation", { name: "Pagination" })).toBeNull();
    // The search box kept its focus through the refetch.
    expect(screen.getByLabelText("Search memories")).toHaveFocus();
  });

  // Pinned has no server filter: it reads the newest 1,000 and keeps the pinned ones, so a
  // pinned memory past the first page shows, with no pager over pages it emptied.
  it("Pinned reads past the page on screen", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, limit) =>
      limit === 1000
        ? {
            entries: [{ ...entry("old_pin", []), pinned: true }, entry("both", ["Alerts", "storage"])],
            total: 250,
            today: 0,
            truncated: false,
          }
        : {
            entries: [{ ...entry("both", ["Alerts", "storage"]), pinned: true }],
            total: 250,
            today: 0,
            truncated: false,
          },
    );
    await openMemories();
    await userEvent.click(within(screen.getByLabelText("Search and filters")).getByRole("button", { name: "Status" }));
    await waitFor(() => expect(item("pinned")).not.toBeNull());
    await userEvent.click(item("pinned") as HTMLElement);
    expect(await screen.findByText("old_pin")).toBeInTheDocument();
    expect(vi.mocked(listMemories).mock.calls.at(-1)?.[1]).toBe(1000);
    expect(screen.queryByRole("navigation", { name: "Memories pages" })).toBeNull();
  });

  // A filter the server matches nothing for keeps the toolbar, so the filter can be cleared;
  // it is not the empty-namespace page.
  it("a filter that matches nothing keeps the toolbar and offers to clear it", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.tags?.length
        ? { entries: [], total: 0, today: 0, truncated: false }
        : { entries: [entry("both", ["Alerts", "storage"])], total: 1, today: 1, truncated: false },
    );
    await openMemories();
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:nomatch");
    // One line, not the big state: "No memories match tag:nomatch" and Clear filters.
    expect(await screen.findByText((_, el) => el?.textContent === "No memories match tag:nomatch")).toBeInTheDocument();
    expect(screen.queryByText("No memories yet")).toBeNull();
    expect(screen.getByLabelText("Search memories")).toHaveValue("tag:nomatch");
    await userEvent.click(screen.getAllByRole("button", { name: "Clear filters" }).at(-1)!);
    expect(await screen.findByText("both")).toBeInTheDocument();
  });

  // The page waits for the server's answer before it says "Nothing matches", and a tag chip
  // reads at once, without the typing delay.
  it("keeps the rows up while a filtered read is in flight, and a tag chip asks at once", async () => {
    let answer: (v: never) => void = () => {};
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.tags?.length
        ? await new Promise<never>((r) => (answer = r))
        : {
            entries: [entry("both", ["Alerts", "storage"]), entry("storage_only", ["storage"])],
            total: 2,
            today: 2,
            truncated: false,
          },
    );
    await openMemories();
    await userEvent.click(within(card("storage_only")).getByRole("button", { name: "Filter by tag storage" }));
    await waitFor(() => expect(vi.mocked(listMemories).mock.calls.at(-1)?.[2]).toMatchObject({ tags: ["storage"] }), {
      timeout: 150,
    });
    expect(screen.queryByText(/^No memories match/)).toBeNull();
    answer({ entries: [entry("storage_only", ["storage"])], total: 1, today: 1, truncated: false } as never);
    expect(await screen.findByText("storage_only")).toBeInTheDocument();
  });

  // The server matches an agent exactly, so a typed agent is sent as the list has seen it
  // written, whatever case it was typed in.
  it("sends a typed agent the way the list has seen it written", async () => {
    await openMemories();
    await userEvent.type(screen.getByLabelText("Search memories"), "agent:Claude-Desktop");
    await waitFor(() =>
      expect(vi.mocked(listMemories).mock.calls.at(-1)?.[2]).toMatchObject({ agent: "claude-desktop" }),
    );
  });
});
