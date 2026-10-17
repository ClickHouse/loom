import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry } from "../api";
import { useStore } from "../lib/state";
import { Recent } from "./Recent";

// "Recently deleted" swaps the list for the 14-day grace set; the page reloads its rows,
// and the toggle a keyboard user pressed keeps focus and one name.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { listMemories } from "../api";

const entry = (key: string, tombstoned = false): MemoryListEntry => ({
  memory_key: key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: new Date().toISOString(),
  expires_at: null,
  pinned: false,
  tags: [],
  value: `value of ${key}`,
  ...(tombstoned ? { tombstoned_at: new Date().toISOString() } : {}),
});

describe("Recently deleted toggle", () => {
  beforeEach(() => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.include_tombstoned
        ? { entries: [entry("gone/key", true)], total: 1, truncated: false, today: 0 }
        : { entries: [entry("live/key")], total: 1, truncated: false, today: 1 },
    );
    useStore.setState({
      authed: true,
      whoami: { authenticated: false },
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "memories",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, identity: null });
  });

  it("is a choice between live and recently deleted that keeps focus through the reload", async () => {
    render(<Recent />);
    const shown = await screen.findByRole("group", { name: "Memories shown" });
    expect(within(shown).getByRole("button", { name: "Live" })).toHaveAttribute("aria-pressed", "true");
    within(shown).getByRole("button", { name: "Recently deleted" }).focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByText("gone/key");
    const deleted = within(screen.getByRole("group", { name: "Memories shown" })).getByRole("button", {
      name: "Recently deleted",
    });
    await waitFor(() => expect(deleted).toHaveFocus());
    expect(deleted).toHaveAttribute("aria-pressed", "true");
  });
});
