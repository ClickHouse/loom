import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
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
    restoreMemory: vi.fn(async () => ({ restored: true })),
    pinMemory: vi.fn(),
    deleteMemory: vi.fn(),
    isAuthDisabled: () => false,
  };
});
import { listMemories, restoreMemory, type MyNamespaceEntry } from "../api";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";

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

  // The page reads the filter the cards follow: a `tag:` still being typed changes nothing
  // until the typing delay has passed.
  it("keeps the empty state while a tag is typed", async () => {
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
    await screen.findByText("No recently deleted memories");
    await userEvent.type(screen.getByLabelText("Search memories"), "tag:x");
    expect(screen.getByText("No recently deleted memories")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("No recently deleted memories")).toBeNull());
    // The status is a filter too, so the row names the filters in force, not the tag alone.
    expect(await screen.findByText(/No memories match/)).toHaveTextContent("No memories match the filters in force");
  });

  // The UTC rule itself is daysLeftToRestore's (lib/format.test.ts).
  it("a deleted row says how many days are left to restore it", async () => {
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
    }
  });
});

describe("Memories list writes by role", () => {
  const grant = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
    namespace: "ns-a",
    role,
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
  });
  function seed(role: MyNamespaceEntry["role"]) {
    resetNamespaceRoleCache();
    vi.mocked(restoreMemory).mockClear();
    vi.mocked(listMemories).mockImplementation(async (_id, _limit, opts) =>
      opts?.include_tombstoned
        ? { entries: [entry("gone/key", true)], total: 1, truncated: false, today: 0 }
        : { entries: [entry("live/key")], total: 1, truncated: false, today: 1 },
    );
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "p@example.com", org: "dev", roles: ["USER"], namespaces: ["ns-a"] },
      myNamespaces: [grant(role)],
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "memories",
    });
  }
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });
  async function showDeleted() {
    await userEvent.click(screen.getAllByRole("button", { name: "Status" })[0]);
    await waitFor(() => expect(item("deleted")).not.toBeNull());
    await userEvent.click(item("deleted") as HTMLElement);
    await screen.findByText("gone/key");
  }

  it("shows a viewer Restore unavailable with why, sends nothing, and offers no selection", async () => {
    seed("viewer");
    render(<Recent />);
    await screen.findByText("live/key");
    // The selection holds only Pin and Delete, which a viewer cannot call.
    expect(screen.queryByRole("checkbox", { name: "Select live/key" })).toBeNull();
    await showDeleted();
    const restore = screen.getByRole("button", { name: "Restore" });
    expect(restore).toHaveAttribute("aria-disabled", "true");
    expect(restore).toHaveAccessibleDescription("Needs writer on ns-a. An admin of it can grant that.");
    await userEvent.click(restore);
    expect(restoreMemory).not.toHaveBeenCalled();
  });

  it("drops the selection bar when the role falls short with rows selected", async () => {
    seed("writer");
    render(<Recent />);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select live/key" }));
    expect(await screen.findByRole("button", { name: /^Delete/ })).toBeInTheDocument();
    act(() => useStore.setState({ myNamespaces: [grant("viewer")] }));
    await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Select live/key" })).toBeNull());
    expect(screen.queryByRole("button", { name: /^Delete/ })).toBeNull();
  });

  it("lets a writer restore a deleted memory and select rows", async () => {
    seed("writer");
    render(<Recent />);
    await screen.findByText("live/key");
    expect(screen.getByRole("checkbox", { name: "Select live/key" })).toBeInTheDocument();
    await showDeleted();
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(restoreMemory).toHaveBeenCalled());
  });
});
