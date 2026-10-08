import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Recent } from "./Recent";
import { must } from "../test/must";

// The Memories view's one-per-namespace conflicts.list read (for the cards' conflict
// chips) fires only when the role here is known to allow it; when the role is unknown the
// cards carry no chip rather than draw an AUTH_INSUFFICIENT per load.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});
import { conflictKeys, listMemories, listMine } from "../api";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["reporter-public"],
};

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[] | null) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: "dev", namespace: "reporter-public", agent: "loom-ui" },
    view: "memories",
  });
}

async function settled() {
  // The memories list is the read every fixture makes; once it has landed
  // any conflicts read the effect was going to make has been issued too.
  await waitFor(() => expect(listMemories).toHaveBeenCalled());
  await Promise.resolve();
}

describe("Recent conflicts read", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("a viewer with an empty list_mine (unknown role) never calls conflicts.list", async () => {
    seed(viewer, []);
    render(<Recent />);
    await settled();
    expect(conflictKeys).not.toHaveBeenCalled();
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument();
  });

  it("a viewer whose list_mine throws (unknown role) never calls conflicts.list", async () => {
    vi.mocked(listMine).mockRejectedValue(new Error("403"));
    seed(viewer, null);
    render(<Recent />);
    await settled();
    await waitFor(() => expect(listMine).toHaveBeenCalled());
    await Promise.resolve();
    expect(conflictKeys).not.toHaveBeenCalled();
  });

  it("a viewer with a jwt row on the namespace reads conflicts — the registry allows VIEWER", async () => {
    seed(viewer, [row("reporter-public", "jwt")]);
    render(<Recent />);
    await settled();
    await waitFor(() => expect(conflictKeys).toHaveBeenCalledTimes(1));
  });

  it("an admin claim reads conflicts even when list_mine failed", async () => {
    vi.mocked(listMine).mockRejectedValue(new Error("403"));
    seed({ ...viewer, roles: ["ADMIN"] }, null);
    render(<Recent />);
    await settled();
    await waitFor(() => expect(conflictKeys).toHaveBeenCalledTimes(1));
  });
});

// The 8 s refresh sends the namespace version the last full answer carried; an
// `unchanged` answer keeps that version for the next tick and repaints nothing.
describe("Recent change-driven refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("polls with known_version and holds it across an unchanged answer", async () => {
    vi.mocked(listMemories)
      .mockResolvedValueOnce({ entries: [], truncated: false, total: 0, today: 0, namespace_version: "7" })
      .mockResolvedValueOnce({
        entries: [],
        truncated: false,
        total: 0,
        today: 0,
        namespace_version: "7",
        unchanged: true,
      })
      .mockResolvedValueOnce({ entries: [], truncated: false, total: 0, today: 0, namespace_version: "9" })
      .mockResolvedValue({
        entries: [],
        truncated: false,
        total: 0,
        today: 0,
        namespace_version: "9",
        unchanged: true,
      });
    seed({ ...viewer, roles: ["ADMIN"] }, null);
    render(<Recent />);
    await waitFor(() => expect(listMemories).toHaveBeenCalledTimes(1));
    const optsOf = (n: number) => must(vi.mocked(listMemories).mock.calls[n][2], `memory.list call ${n} opts`);
    expect(optsOf(0).known_version).toBeUndefined(); // the first load asks for everything

    await vi.advanceTimersByTimeAsync(8000);
    await waitFor(() => expect(listMemories).toHaveBeenCalledTimes(2));
    expect(optsOf(1).known_version).toBe("7"); // sends the version it has

    await vi.advanceTimersByTimeAsync(8000);
    await waitFor(() => expect(listMemories).toHaveBeenCalledTimes(3));
    expect(optsOf(2).known_version).toBe("7"); // unchanged did not move it

    await vi.advanceTimersByTimeAsync(8000);
    await waitFor(() => expect(listMemories).toHaveBeenCalledTimes(4));
    expect(optsOf(3).known_version).toBe("9"); // a full answer did
  });
});
