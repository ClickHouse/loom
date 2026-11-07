import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { MemoryInspector } from "./MemoryInspector";

// The drawer's lifecycle actions write through the row's own identity, and each is
// offered only to a role the server lets call it.

const LIVE: MemoryDetail = {
  found: true,
  memory_id: "m-live",
  session_id: "sess-7",
  value: "oat milk, no sugar",
  memory_type: "preference",
  confidence: 0.8,
  version: 3,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: ["coffee"],
  status: "active",
  agent: "claude-desktop",
  user_id: "alice",
};

vi.mock("./Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("./ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true), ConfirmViewport: () => null }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => LIVE),
    setMemory: vi.fn(),
    deleteMemory: vi.fn(async () => ({ tombstoned: true, version: 4 })),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
vi.mock("../api/memoryLifecycle", () => ({
  lockMemory: vi.fn(async () => ({ locked: true })),
  unlockMemory: vi.fn(async () => ({ unlocked: true, reason: "" })),
  rejectMemory: vi.fn(async () => ({ rejected: true })),
  shareMemory: vi.fn(async () => ({ shared_memory_key: "coffee_pref" })),
  unshareMemory: vi.fn(async () => ({ demoted: true })),
  redactMemoryVersion: vi.fn(async () => ({ redacted: true, refused_head: false })),
  recordMemoryOutcome: vi.fn(async () => ({ recorded: true, quarantined: false })),
  memoryEvidence: vi.fn(async () => ({
    found: true,
    truncated: false,
    items: [
      {
        evidence_id: "e1",
        scope: "cited",
        role: "user",
        observed_at: null,
        content: "I take my coffee with oat milk",
        span: "oat milk",
      },
    ],
  })),
  memoryBacklinks: vi.fn(async () => ({
    found: true,
    digests: [],
    links_in: [{ memory_key: "morning_routine", relation: "mentions", created_at: "" }],
    links_out: [],
  })),
}));
import * as api from "../api";
import * as lc from "../api/memoryLifecycle";
import { confirmAction } from "./ConfirmDialog";
import { toast } from "./Toast";

const grant = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const person: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["team"],
};
const identity = { org: "dev", namespace: "team", agent: "loom-ui" };
const row = { ...identity, agent: "claude-desktop", user_id: "alice", session_id: "sess-7" };

async function open(role: MyNamespaceEntry["role"], detail: MemoryDetail = LIVE) {
  vi.mocked(api.getMemory).mockResolvedValue(detail);
  useStore.setState({ authed: true, whoami: person, myNamespaces: [grant(role)], identity });
  render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />);
  await screen.findByText("oat milk, no sugar");
}

async function menu() {
  await userEvent.click(screen.getByRole("button", { name: "More actions" }));
  return screen.findByRole("menu", { name: "More actions" });
}

beforeEach(() => {
  vi.clearAllMocks();
  resetNamespaceRoleCache();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  useStore.setState({ whoami: null, myNamespaces: null, identity: null });
});

describe("MemoryInspector lifecycle actions", () => {
  it("offers a viewer no lifecycle action", async () => {
    await open("viewer");
    const items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    for (const label of [
      "Lock…",
      "Mark as wrong…",
      "Share with every agent",
      "Stop sharing",
      "This helped",
      "This misled",
    ])
      expect(items).not.toContain(label);
    cleanup();

    await open("viewer", { ...LIVE, lock_mode: "read_only" });
    const locked = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(locked).not.toContain("Unlock");
  });

  it("writes an expiry and an edit to the row it shows, not under the drawer's identity", async () => {
    vi.mocked(api.setMemory).mockResolvedValue({ version: 4, pinned: false });
    await open("writer");
    await userEvent.click(screen.getByRole("button", { name: "Change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "1 hour" }));
    await waitFor(() =>
      expect(api.setMemory).toHaveBeenCalledWith(
        row,
        "coffee_pref",
        LIVE.value,
        expect.objectContaining({ ttlSeconds: 3600 }),
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.click(await screen.findByRole("button", { name: "Save new version" }));
    await waitFor(() => expect(api.setMemory).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.setMemory).mock.calls[1][0]).toEqual(row);
  });

  it("deletes the row it shows, and says so when nothing was deleted", async () => {
    await open("writer");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteMemory).toHaveBeenCalledWith(row, "coffee_pref"));
    cleanup();

    vi.mocked(api.deleteMemory).mockResolvedValueOnce({ tombstoned: false, version: 0 });
    await open("writer");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("This memory is no longer live", "info"));
  });

  it("offers no write a lock refuses", async () => {
    await open("writer", { ...LIVE, lock_mode: "read_only" });
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    // No lock gates tags: a tag added here is written to the row.
    vi.mocked(api.setMemory).mockResolvedValue({ version: 4, pinned: false });
    await userEvent.type(screen.getByLabelText("Add a tag"), "ops{Enter}");
    await waitFor(() =>
      expect(api.setMemory).toHaveBeenCalledWith(
        row,
        "coffee_pref",
        LIVE.value,
        expect.objectContaining({ tags: ["coffee", "ops"] }),
      ),
    );
    const items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(items).not.toContain("Rename");
    cleanup();

    await open("writer", { ...LIVE, lock_mode: "no_delete" });
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("offers sharing to an admin only, and no share of a profile or a high memory", async () => {
    await open("writer");
    let items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(items).not.toContain("Share with every agent");
    expect(items).not.toContain("Stop sharing");
    cleanup();

    await open("admin", { ...LIVE, sensitivity: "high" });
    items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(items).not.toContain("Share with every agent");
    expect(items).toContain("Stop sharing");
  });

  it("says an outcome was not recorded", async () => {
    vi.mocked(lc.recordMemoryOutcome).mockResolvedValueOnce({ recorded: false, quarantined: false });
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "This helped" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Couldn't record that", "error"));
  });

  it("leaves the drawer's shortcuts alone while a reason dialog is open", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    within(dialog).getByRole("button", { name: "Cancel" }).focus();
    await userEvent.keyboard("d");
    expect(confirmAction).not.toHaveBeenCalled();
  });

  it("says a lock is gone when the unlock finds none", async () => {
    vi.mocked(lc.unlockMemory).mockResolvedValueOnce({ unlocked: false, reason: "not_locked" });
    await open("admin", { ...LIVE, lock_mode: "read_only" });
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Unlock" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("This memory carried no lock", "info"));
  });

  it("says why a share was refused", async () => {
    vi.mocked(lc.shareMemory).mockRejectedValueOnce(new Error("a shared memory already holds this key"));
    await open("admin");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Share with every agent" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringMatching(/^Share failed: /), "error"));
  });

  it("names the lock holder and when it lapses in the lock's tip", async () => {
    await open("writer", {
      ...LIVE,
      lock_mode: "no_delete",
      lock_reason: "legal hold",
      locked_by: "counsel@example.com",
      lock_until: "2030-01-01T00:00:00Z",
    });
    await userEvent.hover(within(screen.getByRole("contentinfo")).getByText("No delete"));
    expect(await screen.findAllByText(/legal hold · by counsel@example.com · until /)).not.toHaveLength(0);
  });

  it("locks with a mode and a reason", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    await userEvent.click(within(dialog).getByRole("radio", { name: "No delete" }));
    await userEvent.type(within(dialog).getByLabelText("Reason"), "legal hold");
    await userEvent.click(within(dialog).getByRole("button", { name: "Lock" }));
    await waitFor(() => expect(lc.lockMemory).toHaveBeenCalledWith(row, "coffee_pref", "no_delete", "legal hold"));
  });

  it("closes an open edit when a read only lock lands, and says so", async () => {
    await open("writer");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("button", { name: "Save new version" })).toBeInTheDocument();
    vi.mocked(api.getMemory).mockResolvedValue({ ...LIVE, lock_mode: "read_only" });
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "legal hold");
    await userEvent.click(within(dialog).getByRole("button", { name: "Lock" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Save new version" })).toBeNull());
    expect(api.setMemory).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith("Locked read only: the open edit was closed", "info");
  });

  it("closes an open rename when a read only lock lands, and names it", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Rename" }));
    expect(await screen.findByTestId("rename-form")).toBeInTheDocument();
    vi.mocked(api.getMemory).mockResolvedValue({ ...LIVE, lock_mode: "read_only" });
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "legal hold");
    await userEvent.click(within(dialog).getByRole("button", { name: "Lock" }));
    await waitFor(() => expect(screen.queryByTestId("rename-form")).toBeNull());
    expect(toast).toHaveBeenCalledWith("Locked read only: the open rename was closed", "info");
  });

  it("carries no open edit to an unlocked next key, and a late save leaves its editor alone", async () => {
    let finish: (v: { version: number; pinned: boolean }) => void = () => {};
    vi.mocked(api.setMemory).mockImplementationOnce(() => new Promise((r) => (finish = r)));
    vi.mocked(api.getMemory).mockResolvedValue(LIVE);
    useStore.setState({ authed: true, whoami: person, myNamespaces: [grant("writer")], identity });
    const view = render(
      <MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("oat milk, no sugar");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.click(await screen.findByRole("button", { name: "Save new version" }));
    const other = { ...LIVE, memory_id: "m-other", value: "black" };
    vi.mocked(api.getMemory).mockImplementation(async (_id, key) => (key === "morning_routine" ? other : LIVE));
    view.rerender(
      <MemoryInspector identity={identity} memoryKey="morning_routine" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("black");
    // The editor did not follow the drawer to the next key.
    expect(screen.queryByRole("button", { name: "Save new version" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await act(async () => finish({ version: 4, pinned: false }));
    // The late answer neither closes the next key's editor nor brings the key left back.
    expect(screen.getByRole("button", { name: "Save new version" })).toBeInTheDocument();
    expect(screen.queryByText("oat milk, no sugar")).toBeNull();
    expect(api.setMemory).toHaveBeenCalledTimes(1);
  });

  it("offers no Edit while a rename is open, by button or by key", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Rename" }));
    await screen.findByTestId("rename-form");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    screen.getByRole("dialog").focus();
    await userEvent.keyboard("e");
    expect(screen.queryByRole("button", { name: "Save new version" })).toBeNull();
  });

  it("drops an open edit when the drawer moves to another key, without a lock toast", async () => {
    vi.mocked(api.getMemory).mockResolvedValue(LIVE);
    useStore.setState({ authed: true, whoami: person, myNamespaces: [grant("writer")], identity });
    const view = render(
      <MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("oat milk, no sugar");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("button", { name: "Save new version" })).toBeInTheDocument();
    // The next key is locked read only from the start: nothing landed while it was open.
    vi.mocked(api.getMemory).mockResolvedValue({
      ...LIVE,
      memory_id: "m-other",
      value: "black",
      lock_mode: "read_only",
    });
    view.rerender(
      <MemoryInspector identity={identity} memoryKey="morning_routine" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("black");
    expect(screen.queryByRole("button", { name: "Save new version" })).toBeNull();
    expect(toast).not.toHaveBeenCalledWith(expect.stringMatching(/^Locked read only/), "info");
  });

  it("holds the reason dialog open while its call runs", async () => {
    let finish: (v: { locked: boolean }) => void = () => {};
    vi.mocked(lc.lockMemory).mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "legal hold");
    await userEvent.click(within(dialog).getByRole("button", { name: "Lock" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());
    await userEvent.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Lock memory" })).toBeInTheDocument();
    finish({ locked: true });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Lock memory" })).toBeNull());
  });

  it("shows the lock and lets an admin, not a writer, lift it", async () => {
    const locked = { ...LIVE, lock_mode: "read_only", lock_reason: "legal hold" };
    await open("writer", locked);
    expect(screen.getByRole("contentinfo")).toHaveTextContent("Read only");
    const items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(items).not.toContain("Unlock");
    expect(items).not.toContain("Lock…");
    // A locked memory is not retired from the drawer.
    expect(items).not.toContain("Mark as wrong…");
    cleanup();

    await open("admin", locked);
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Unlock" }));
    await waitFor(() => expect(lc.unlockMemory).toHaveBeenCalledWith(row, "coffee_pref"));
  });

  it("marks a memory as wrong with the reason", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Mark as wrong…" }));
    const dialog = await screen.findByRole("dialog", { name: "Mark as wrong" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "she switched to soy");
    await userEvent.click(within(dialog).getByRole("button", { name: "Mark as wrong" }));
    await waitFor(() => expect(lc.rejectMemory).toHaveBeenCalledWith(row, "coffee_pref", "she switched to soy"));
  });

  it("shares with every agent and stops sharing, for an admin, each after a confirmation", async () => {
    await open("admin");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Share with every agent" }));
    await waitFor(() => expect(lc.shareMemory).toHaveBeenCalledWith(row, "coffee_pref"));
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Stop sharing" }));
    await waitFor(() => expect(lc.unshareMemory).toHaveBeenCalledWith(identity, "coffee_pref"));
    expect(confirmAction).toHaveBeenCalledTimes(2);
    expect(vi.mocked(confirmAction).mock.calls[1][0]).toMatchObject({ title: "Stop sharing coffee_pref in team?" });
  });

  it("shares nothing when the confirmation is declined", async () => {
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    await open("admin");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Share with every agent" }));
    await waitFor(() => expect(confirmAction).toHaveBeenCalled());
    expect(lc.shareMemory).not.toHaveBeenCalled();
  });

  it("reads the evidence of the memory it moved to, under that memory's own identity", async () => {
    const other = { ...LIVE, memory_id: "m-other", value: "black", agent: "cursor", user_id: "bob", session_id: "s9" };
    vi.mocked(api.getMemory).mockImplementation(async (_id, key) => (key === "coffee_pref" ? LIVE : other));
    useStore.setState({ authed: true, whoami: person, myNamespaces: [grant("viewer")], identity });
    const { rerender } = render(
      <MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("oat milk, no sugar");
    await userEvent.click(screen.getByRole("button", { name: /Evidence/ }));
    await waitFor(() => expect(lc.memoryEvidence).toHaveBeenCalledWith(row, "coffee_pref"));
    rerender(<MemoryInspector identity={identity} memoryKey="tea_pref" onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("black");
    await waitFor(() => expect(lc.memoryEvidence).toHaveBeenCalledTimes(2));
    expect(lc.memoryEvidence).toHaveBeenLastCalledWith(
      { ...identity, agent: "cursor", user_id: "bob", session_id: "s9" },
      "tea_pref",
    );
  });

  it("reads the evidence and the links only when their sections open", async () => {
    await open("viewer");
    expect(lc.memoryEvidence).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Evidence/ }));
    expect(await screen.findByText("oat milk")).toBeInTheDocument();
    // Under the row's own identity: an extracted row carries its agent, user and session.
    expect(lc.memoryEvidence).toHaveBeenCalledWith(row, "coffee_pref");
    await userEvent.click(screen.getByRole("button", { name: /Links/ }));
    expect(await screen.findByRole("button", { name: "morning_routine" })).toBeInTheDocument();
    expect(screen.getByText("mentions from")).toBeInTheDocument();
  });

  it("redacts a past version by its exact version and the reason, for an admin", async () => {
    const v = (version_text: string, value: string) => ({
      memory_id: "m1",
      version: Number(version_text),
      version_text,
      value,
      created_at: "2026-09-18T09:00:00Z",
      written_at: "2026-09-18T09:00:00Z",
      confidence: 0.8,
      pinned: false,
    });
    vi.mocked(api.memoryHistory).mockResolvedValue({
      rows: [v("1790000000000000002", "oat milk"), v("1790000000000000001", "card 4111 1111")],
    } as never);
    await open("admin");
    await userEvent.click(screen.getByRole("button", { name: /History/ }));
    await userEvent.click(await screen.findByText(/card 4111/));
    await userEvent.click(await screen.findByRole("button", { name: "Redact v1" }));
    const dialog = await screen.findByRole("dialog", { name: "Redact v1" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "card number");
    await userEvent.click(within(dialog).getByRole("button", { name: "Redact" }));
    await waitFor(() =>
      expect(lc.redactMemoryVersion).toHaveBeenCalledWith("dev", "m1", "1790000000000000001", "card number"),
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Redact v1" })).toBeNull());
  });

  it("keeps the redact dialog open with the reason when the version is live", async () => {
    vi.mocked(lc.redactMemoryVersion).mockResolvedValueOnce({ redacted: false, refused_head: true });
    vi.mocked(api.memoryHistory).mockResolvedValue({
      rows: [
        {
          memory_id: "m1",
          version: 2,
          version_text: "2",
          value: "oat milk",
          created_at: "2026-09-18T09:00:00Z",
          written_at: "2026-09-18T09:00:00Z",
          confidence: 0.8,
          pinned: false,
        },
        {
          memory_id: "m1",
          version: 1,
          version_text: "1",
          value: "card 4111 1111",
          created_at: "2026-09-18T09:00:00Z",
          written_at: "2026-09-18T09:00:00Z",
          confidence: 0.8,
          pinned: false,
        },
      ],
    } as never);
    await open("admin");
    await userEvent.click(screen.getByRole("button", { name: /History/ }));
    await userEvent.click(await screen.findByText(/card 4111/));
    await userEvent.click(await screen.findByRole("button", { name: "Redact v1" }));
    const dialog = await screen.findByRole("dialog", { name: "Redact v1" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "card number");
    await userEvent.click(within(dialog).getByRole("button", { name: "Redact" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("live version");
  });

  it("records a writer's verdict on the memory", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "This misled" }));
    // By id: the op resolves a key in the `-` session only, and this row has its own.
    await waitFor(() => expect(lc.recordMemoryOutcome).toHaveBeenCalledWith(identity, "m-live", "negative"));
  });

  it("reads the memory again when a bad outcome rejects it", async () => {
    vi.mocked(lc.recordMemoryOutcome).mockResolvedValueOnce({ recorded: true, quarantined: true });
    await open("writer");
    const reads = vi.mocked(api.getMemory).mock.calls.length;
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "This misled" }));
    await waitFor(() => expect(vi.mocked(api.getMemory).mock.calls.length).toBe(reads + 1));
    expect(vi.mocked(api.getMemory).mock.calls.at(-1)?.[2]).toEqual({ include_recall_breakdown: true });
  });
});
