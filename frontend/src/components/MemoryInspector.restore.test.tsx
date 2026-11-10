import { cleanup, render, screen, waitFor } from "../test/rtl";
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
  superseded_by: "m-city-4",
  memory_id: "m-city-3",
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
import { toast, toastError } from "./Toast";

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
        // Only the retirement on screen: a delete since is left as it is.
        if_superseded_by: "m-city-4",
        only_if_superseded: true,
      }),
    );
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored", "ok"));
    expect(onChanged).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Restore" })).toBeNull());
  });

  it("guards by status when the successor is out of the reader's view", async () => {
    seed("writer");
    vi.mocked(getMemory).mockResolvedValue({ ...SUPERSEDED, superseded_by: "" });
    await open();
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() =>
      expect(restoreMemory).toHaveBeenCalledWith({ ...identity, agent: "claude-desktop" }, "user_city", {
        user_id: "u1",
        session_id: "s1",
        if_superseded_by: "",
        only_if_superseded: true,
      }),
    );
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

  // Opens user_city, clicks Restore, then moves the drawer to user_job before the
  // restore answers. Returns the callbacks that answer it and the render.
  async function restoreThenMove(onChanged: () => void) {
    const job: MemoryDetail = { ...SUPERSEDED, value: "works at Acme", memory_id: "m-job-1", status: "active" };
    vi.mocked(getMemory).mockImplementation(async (_id, key) => (key === "user_job" ? job : SUPERSEDED));
    const view = render(
      <MemoryInspector identity={identity} memoryKey="user_city" onClose={() => {}} onChanged={onChanged} />,
    );
    await screen.findByText("lives in Lisbon");
    let finish: (v: { restored: boolean }) => void = () => {};
    let fail: (e: Error) => void = () => {};
    vi.mocked(restoreMemory).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          finish = resolve;
          fail = reject;
        }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    view.rerender(
      <MemoryInspector identity={identity} memoryKey="user_job" onClose={() => {}} onChanged={onChanged} />,
    );
    await screen.findByText("works at Acme");
    vi.mocked(getMemory).mockClear();
    return { finish, fail, view };
  }

  it("names the key of a restore that lands after the drawer moved to another memory", async () => {
    seed("writer");
    const onChanged = vi.fn();
    const { finish } = await restoreThenMove(onChanged);
    finish({ restored: true });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored user_city", "ok"));
    // The drawer keeps showing the memory it moved to.
    expect(screen.getByText("works at Acme")).toBeTruthy();
    expect(screen.queryByText("lives in Lisbon")).toBeNull();
    expect(getMemory).not.toHaveBeenCalled();
    // The restore did land, so the list behind the drawer is refreshed.
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("shows the restored row when the drawer is back on the memory as the restore lands", async () => {
    seed("writer");
    const { finish, view } = await restoreThenMove(() => {});
    // Back to user_city: its load read answers the superseded row before the restore does.
    // The parent rebuilds the identity object on the way back, as App does per agent.
    vi.mocked(getMemory).mockResolvedValue(SUPERSEDED);
    view.rerender(
      <MemoryInspector identity={{ ...identity }} memoryKey="user_city" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("lives in Lisbon");
    expect(screen.getByRole("button", { name: "Restore" })).toBeTruthy();
    vi.mocked(getMemory).mockResolvedValue({ ...SUPERSEDED, status: "active" });
    finish({ restored: true });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored user_city", "ok"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Restore" })).toBeNull());
  });

  it("names the key of a restore that fails after the drawer moved to another memory", async () => {
    seed("writer");
    const { fail } = await restoreThenMove(() => {});
    fail(new Error("network down"));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Restore of user_city failed",
        expect.objectContaining({ message: "network down" }),
      ),
    );
    expect(screen.getByText("works at Acme")).toBeTruthy();
  });

  it("names the key of a restore that finds nothing after the drawer moved to another memory", async () => {
    seed("writer");
    const { finish } = await restoreThenMove(() => {});
    finish({ restored: false });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Nothing to restore for user_city", "info"));
    expect(getMemory).not.toHaveBeenCalled();
  });

  it("names the key of a restore that lands after the drawer closed", async () => {
    seed("writer");
    const onChanged = vi.fn();
    const view = render(
      <MemoryInspector identity={identity} memoryKey="user_city" onClose={() => {}} onChanged={onChanged} />,
    );
    await screen.findByText("lives in Lisbon");
    let finish: (v: { restored: boolean }) => void = () => {};
    vi.mocked(restoreMemory).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    view.unmount();
    vi.mocked(getMemory).mockClear();
    finish({ restored: true });
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored user_city", "ok"));
    // No drawer is left to show a re-read in.
    expect(getMemory).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalledWith("Restored", "ok");
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("reports a landed restore even when the re-read fails", async () => {
    seed("writer");
    const onChanged = vi.fn();
    await open(onChanged);
    vi.mocked(restoreMemory).mockResolvedValue({ restored: true, from_status: "superseded" });
    vi.mocked(getMemory).mockRejectedValue(new Error("network down"));
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored", "ok"));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Reload failed", expect.objectContaining({ message: "network down" })),
    );
    expect(onChanged).toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalledWith(expect.stringContaining("Restore failed"), expect.anything());
  });

  it("re-reads the row and the list when the restore finds nothing, so Restore is not offered again", async () => {
    seed("writer");
    const onChanged = vi.fn();
    await open(onChanged);
    vi.mocked(restoreMemory).mockResolvedValue({ restored: false, from_status: "" });
    // Restored by someone else since the drawer read it.
    vi.mocked(getMemory).mockResolvedValue({ ...SUPERSEDED, status: "active" });
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Nothing to restore", "info"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Restore" })).toBeNull());
    // The list behind the drawer still shows the row as it was read.
    expect(onChanged).toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalledWith("Restored", "ok");
  });

  it("offers Restore on the next memory while a restore of the last one is pending", async () => {
    seed("writer");
    const other: MemoryDetail = { ...SUPERSEDED, value: "works at Acme", memory_id: "m-job-1" };
    vi.mocked(getMemory).mockImplementation(async (_id, key) => (key === "user_job" ? other : SUPERSEDED));
    const view = render(
      <MemoryInspector identity={identity} memoryKey="user_city" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("lives in Lisbon");
    const finish: Array<(v: { restored: boolean }) => void> = [];
    vi.mocked(restoreMemory).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish.push(resolve);
        }),
    );
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));
    view.rerender(<MemoryInspector identity={identity} memoryKey="user_job" onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("works at Acme");
    const button = await screen.findByRole("button", { name: "Restore" });
    expect(button).not.toBeDisabled();
    await userEvent.click(button);
    expect(button).toBeDisabled();
    // The first memory's reply does not end the second one's restore.
    finish[0]?.({ restored: true });
    await new Promise((r) => setTimeout(r, 20));
    expect(button).toBeDisabled();
  });

  it("offers no Restore to a viewer", async () => {
    seed("viewer");
    await open();
    expect(screen.getByText("Superseded by a newer version")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Restore" })).toBeNull();
  });
});
