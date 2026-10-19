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

// Click UI's Select items carry no option role; they are addressed by value.
const item = (value: string) => document.querySelector<HTMLElement>(`[cui-select-item][data-value="${value}"]`);

describe("Recently deleted status", () => {
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

  // The Status select is Live, Pinned, Conflicted, Verified and Recently
  // deleted, in the toolbar; it replaces the header's Live | Recently deleted toggle. The
  // toolbar stays mounted through the reload, so the select keeps the focus.
  it("is the Recently deleted status, and the select keeps focus through the reload", async () => {
    render(<Recent />);
    await screen.findByText("live/key");
    expect(screen.queryByRole("group", { name: "Memories shown" })).toBeNull();
    const toolbar = screen.getByLabelText("Search and filters");
    const status = within(toolbar).getByRole("button", { name: "Status" });
    expect(status).toHaveTextContent("Live");
    status.focus();
    await userEvent.click(status);
    await waitFor(() => expect(item("deleted")).not.toBeNull());
    for (const value of ["live", "pinned", "verified"]) expect(item(value)).not.toBeNull();
    await userEvent.click(item("deleted") as HTMLElement);
    await screen.findByText("gone/key");
    expect(screen.queryByText("live/key")).toBeNull();
    expect(vi.mocked(listMemories).mock.calls.at(-1)?.[2]).toMatchObject({ include_tombstoned: true });
    const after = within(screen.getByLabelText("Search and filters")).getByRole("button", { name: "Status" });
    expect(after).toBe(status);
    expect(after).toHaveTextContent("Recently deleted");
    await waitFor(() => expect(after).toHaveFocus());
    expect(screen.getByLabelText("Search and filters")).toHaveTextContent("1 match");
  });

  it("says so when nothing was deleted, and goes back to the live list", async () => {
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.include_tombstoned
        ? { entries: [], total: 0, truncated: false, today: 0 }
        : { entries: [entry("live/key")], total: 1, truncated: false, today: 1 },
    );
    render(<Recent />);
    await screen.findByText("live/key");
    await userEvent.click(screen.getAllByRole("button", { name: "Status" })[0]);
    await waitFor(() => expect(item("deleted")).not.toBeNull());
    await userEvent.click(item("deleted") as HTMLElement);
    expect(await screen.findByText("No recently deleted memories")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show live memories" }));
    expect(await screen.findByText("live/key")).toBeInTheDocument();
  });

  // The server's tombstoned_at is a ClickHouse time with no zone, in UTC. Read as local time
  // west of UTC, a row deleted 30 hours ago would be 23 hours old and keep all 14 days.
  it("counts the days left from a zoneless ClickHouse time as UTC", async () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-18T12:00:00Z"));
    try {
      vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
        opts?.include_tombstoned
          ? {
              entries: [{ ...entry("gone/key"), tombstoned_at: "2026-10-17 06:00:00" }],
              total: 1,
              truncated: false,
              today: 0,
            }
          : { entries: [entry("live/key")], total: 1, truncated: false, today: 1 },
      );
      render(<Recent />);
      await screen.findByText("live/key");
      await userEvent.click(screen.getAllByRole("button", { name: "Status" })[0]);
      await waitFor(() => expect(item("deleted")).not.toBeNull());
      await userEvent.click(item("deleted") as HTMLElement);
      expect(await screen.findByText(/13 days left to restore/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      vi.unstubAllEnvs();
    }
  });
});
