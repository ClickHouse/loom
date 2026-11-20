import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { ApiError } from "../lib/errors";
import { MemoryInspector } from "./MemoryInspector";

// Pin writes the inspected row back through memory.pin, which stores a whole new
// version: the row it hands over must be the one the drawer read, tags and all.

const LIVE: MemoryDetail = {
  found: true,
  value: "oat milk, no sugar",
  memory_type: "preference",
  confidence: 0.8,
  version: 3,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: ["coffee"],
  source_trace: "conversation:t1",
  status: "active",
  agent: "claude-desktop",
};

// The real committedAnyway: a committed write's answer counts as done.
vi.mock("./Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./Toast")>()),
  toast: vi.fn(),
  toastError: vi.fn(),
  ToastViewport: () => null,
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => LIVE),
    pinMemory: vi.fn(async () => ({ version: 4, pinned: true })),
    unpinMemory: vi.fn(async () => ({ unpinned: true, reason: "", memory_id: "m1", version: 5 })),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
import { getMemory, pinMemory, unpinMemory } from "../api";

const member: MyNamespaceEntry = {
  namespace: "team-alerts",
  role: "writer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
};

const person: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team-alerts"],
};
const identity = { org: "dev", namespace: "team-alerts", agent: "loom-ui" };

describe("MemoryInspector pin", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    vi.mocked(getMemory).mockResolvedValue(LIVE);
    useStore.setState({
      authed: true,
      whoami: person,
      myNamespaces: [member],
      identity,
      inspectedMemoryKey: "coffee_pref",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("hands memory.pin the row it read, so its tags, confidence and source trace survive", async () => {
    render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />);
    await waitFor(() => expect(getMemory).toHaveBeenCalled());
    await screen.findByText("oat milk, no sugar");

    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));

    await waitFor(() => expect(pinMemory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(pinMemory).mock.calls[0]).toEqual([identity, "coffee_pref", LIVE]);
  });

  it("stays pinned when memory.pin committed with only search behind, and rolls back a failure", async () => {
    // An admin, who may unpin, so the menu shows which state the drawer holds.
    useStore.setState({ myNamespaces: [{ ...member, role: "admin" }] });
    const onChanged = vi.fn();
    const pending = new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: {} });
    vi.mocked(pinMemory).mockRejectedValueOnce(pending);
    render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={onChanged} />);
    await screen.findByText("oat milk, no sugar");
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    // Shown pinned: the menu offers Unpin, not Pin again.
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(await screen.findByRole("menuitem", { name: "Unpin" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    cleanup();
    onChanged.mockClear();
    vi.mocked(pinMemory).mockRejectedValueOnce(new ApiError({ status: 500, code: "INTERNAL", detail: {} }));
    render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={onChanged} />);
    await screen.findByText("oat milk, no sugar");
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
    await waitFor(() => expect(pinMemory).toHaveBeenCalledTimes(2));
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(await screen.findByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("unpins the row it read, by that row's own agent, user and session", async () => {
    useStore.setState({ myNamespaces: [{ ...member, role: "admin" }] });
    vi.mocked(getMemory).mockResolvedValue({ ...LIVE, pinned: true, user_id: "alice", session_id: "s1" });
    render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("oat milk, no sugar");

    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Unpin" }));

    await waitFor(() => expect(unpinMemory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(unpinMemory).mock.calls[0]).toEqual([
      { ...identity, agent: "claude-desktop", user_id: "alice", session_id: "s1" },
      "coffee_pref",
    ]);
  });
});
