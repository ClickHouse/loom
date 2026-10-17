import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";

// A tag on a memory card adds that tag to the filter, and another tag narrows it further
// (every tag must match, as `tag:a tag:b` typed in the search does). The active filters
// sit in the toolbar with the search, so they stay in view as the list scrolls.

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

  it("stacks a second tag onto the first, and shows each in the toolbar", async () => {
    await openMemories();
    await userEvent.click(within(card("alerts_only")).getByRole("button", { name: "Filter by tag: Alerts" }));
    expect(shown()).toEqual(["both", "alerts_only"]);
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag: storage" }));
    expect(shown()).toEqual(["both"]);

    const toolbar = screen.getByLabelText("Search and filters");
    expect(within(toolbar).getByText("tag:Alerts")).toBeInTheDocument();
    expect(within(toolbar).getByText("tag:storage")).toBeInTheDocument();
    expect(screen.getByLabelText("Search memories")).toHaveValue("tag:Alerts tag:storage");
    // On the card, an applied tag reads as pressed.
    expect(within(card("both")).getByRole("button", { name: "Filter by tag: storage" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("takes one tag out from the toolbar or the card, and clears them all", async () => {
    await openMemories();
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag: Alerts" }));
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag: storage" }));
    const toolbar = screen.getByLabelText("Search and filters");

    await userEvent.click(within(toolbar).getByRole("button", { name: "Remove tag:Alerts" }));
    expect(shown()).toEqual(["both", "storage_only"]);
    // Pressing an applied tag on a card takes it out again.
    await userEvent.click(within(card("both")).getByRole("button", { name: "Filter by tag: storage" }));
    expect(shown()).toEqual(["both", "alerts_only", "storage_only"]);

    await userEvent.type(screen.getByLabelText("Search memories"), "tag:alerts tag:storage");
    await userEvent.click(within(toolbar).getByRole("button", { name: "Clear filters" }));
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
});
