import { cleanup, render, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { MemoryInspector } from "./MemoryInspector";

// The drawer's conflicts.list read (the "needs review" badge) fires only when the role
// here is known to allow it. An unknown role sends nothing and the badge is simply
// absent.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => ({
      memory_key: "k1",
      value: "hello",
      memory_type: "semantic",
      pinned: false,
      tags: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      version: 1,
    })),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ rows: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({})),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
import { getMemory, conflictKeys, listMine } from "../api";

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
const identity = { org: "dev", namespace: "reporter-public", agent: "loom-ui" };

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[] | null) {
  useStore.setState({ authed: true, whoami, myNamespaces: mine, identity });
}

function open() {
  return render(<MemoryInspector identity={identity} memoryKey="k1" onClose={() => {}} onChanged={() => {}} />);
}

async function settled() {
  await waitFor(() => expect(getMemory).toHaveBeenCalled());
  await Promise.resolve();
}

describe("MemoryInspector conflicts read", () => {
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
    open();
    await settled();
    expect(conflictKeys).not.toHaveBeenCalled();
  });

  it("a viewer whose list_mine throws (unknown role) never calls conflicts.list", async () => {
    vi.mocked(listMine).mockRejectedValue(new Error("403"));
    seed(viewer, null);
    open();
    await settled();
    await waitFor(() => expect(listMine).toHaveBeenCalled());
    await Promise.resolve();
    expect(conflictKeys).not.toHaveBeenCalled();
  });

  it("a known viewer on the namespace reads conflicts once", async () => {
    seed(viewer, [row("reporter-public", "viewer")]);
    open();
    await settled();
    await waitFor(() => expect(conflictKeys).toHaveBeenCalledTimes(1));
  });

  it("an admin claim reads conflicts even when list_mine failed", async () => {
    vi.mocked(listMine).mockRejectedValue(new Error("403"));
    seed({ ...viewer, roles: ["ADMIN"] }, null);
    open();
    await settled();
    await waitFor(() => expect(conflictKeys).toHaveBeenCalledTimes(1));
  });
});
