import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { MemoryInspector } from "./MemoryInspector";

// A superseded row (Loom retired it, or a later write replaced it) offers Restore to a
// role that reaches `memory.restore`; it calls the op with the row's identity and re-reads.

const SUPERSEDED: MemoryDetail = {
  found: true,
  value: "lives in Lisbon",
  memory_type: "semantic",
  confidence: 1,
  version: 3,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  status: "superseded",
  agent: "claude-desktop",
  user_id: "u1",
  session_id: "s1",
};

vi.mock("./Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => SUPERSEDED),
    restoreMemory: vi.fn(async () => ({ restored: true, from_status: "superseded" })),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ rows: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
import { getMemory, restoreMemory } from "../api";
import { toast } from "./Toast";

const person: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team"],
};
const identity = { org: "dev", namespace: "team", agent: "loom-ui" };

function seed(role: MyNamespaceEntry["role"]) {
  useStore.setState({
    authed: true,
    whoami: person,
    myNamespaces: [
      {
        namespace: "team",
        role,
        status: "active",
        visibility: "private",
        description: "",
        joined_at: null,
        is_new: false,
      },
    ],
    identity,
    inspectedMemoryKey: "user_city",
  });
}

async function open(onChanged = () => {}) {
  render(<MemoryInspector identity={identity} memoryKey="user_city" onClose={() => {}} onChanged={onChanged} />);
  await screen.findByText("lives in Lisbon");
}

describe("MemoryInspector restore of a superseded row", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    vi.mocked(getMemory).mockResolvedValue(SUPERSEDED);
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("restores with the row's identity, toasts and re-reads", async () => {
    seed("writer");
    const onChanged = vi.fn();
    await open(onChanged);
    vi.mocked(getMemory).mockResolvedValue({ ...SUPERSEDED, status: "active" });
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(restoreMemory).toHaveBeenCalledWith({ ...identity, agent: "claude-desktop" }, "user_city", {
        user_id: "u1",
        session_id: "s1",
      }),
    );
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored", "ok"));
    expect(onChanged).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Restore" })).toBeNull());
  });

  it("sends one restore for a double click", async () => {
    seed("writer");
    await open();
    let finish: (v: { restored: boolean }) => void = () => {};
    vi.mocked(restoreMemory).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const button = await screen.findByRole("button", { name: "Restore" });
    await userEvent.click(button);
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(restoreMemory).toHaveBeenCalledTimes(1);
    finish({ restored: false });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Nothing to restore", "info"));
  });

  it("offers no Restore to a viewer", async () => {
    seed("viewer");
    await open();
    expect(screen.getByText("Superseded by a newer version")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
  });
});
