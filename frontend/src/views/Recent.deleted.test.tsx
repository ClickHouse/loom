import { cleanup, render, screen, waitFor } from "../test/rtl";
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

  it("keeps focus and its name, and says whether it is on", async () => {
    render(<Recent />);
    const toggle = await screen.findByRole("button", { name: "Recently deleted" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    toggle.focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByText("gone/key");
    await waitFor(() => expect(screen.getByRole("button", { name: "Recently deleted" })).toHaveFocus());
    expect(screen.getByRole("button", { name: "Recently deleted" })).toHaveAttribute("aria-pressed", "true");
  });
});
