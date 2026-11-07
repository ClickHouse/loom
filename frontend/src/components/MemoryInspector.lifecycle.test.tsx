import { cleanup, render, screen, waitFor, within } from "../test/rtl";
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
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    lockMemory: vi.fn(async () => ({ locked: true })),
    unlockMemory: vi.fn(async () => ({ unlocked: true, reason: "" })),
    touchMemoryExpiry: vi.fn(async () => ({})),
    rejectMemory: vi.fn(async () => ({ rejected: true })),
    shareMemory: vi.fn(async () => ({ shared_memory_key: "coffee_pref" })),
    unshareMemory: vi.fn(async () => ({ demoted: true })),
    redactMemoryVersion: vi.fn(async () => ({ redacted: true, refused_head: false })),
    recordMemoryOutcome: vi.fn(async () => ({})),
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
  };
});
import * as api from "../api";

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
const row = { ...identity, agent: "claude-desktop", user_id: "alice", session_id: "-" };

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
  it("changes only the expiry, through memory.touch on the row", async () => {
    await open("writer");
    await userEvent.click(screen.getByRole("button", { name: "Change" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "1 day" }));
    await waitFor(() => expect(api.touchMemoryExpiry).toHaveBeenCalledWith(row, "coffee_pref", 86400));
    expect(api.setMemory).not.toHaveBeenCalled();
  });

  it("offers a viewer no expiry change and no lifecycle action", async () => {
    await open("viewer");
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    const items = within(await menu())
      .getAllByRole("menuitem")
      .map((i) => i.textContent);
    expect(items).not.toContain("Lock…");
    expect(items).not.toContain("Mark as wrong…");
    expect(items).not.toContain("Share with every agent");
  });

  it("locks with a mode and a reason", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Lock…" }));
    const dialog = await screen.findByRole("dialog", { name: "Lock memory" });
    await userEvent.click(within(dialog).getByRole("radio", { name: "No delete" }));
    await userEvent.type(within(dialog).getByLabelText("Reason"), "legal hold");
    await userEvent.click(within(dialog).getByRole("button", { name: "Lock" }));
    await waitFor(() => expect(api.lockMemory).toHaveBeenCalledWith(row, "coffee_pref", "no_delete", "legal hold"));
  });

  it("holds the reason dialog open while its call runs", async () => {
    let finish: (v: { locked: boolean }) => void = () => {};
    vi.mocked(api.lockMemory).mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
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
    cleanup();

    await open("admin", locked);
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Unlock" }));
    await waitFor(() => expect(api.unlockMemory).toHaveBeenCalledWith(row, "coffee_pref"));
  });

  it("marks a memory as wrong with the reason", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Mark as wrong…" }));
    const dialog = await screen.findByRole("dialog", { name: "Mark as wrong" });
    await userEvent.type(within(dialog).getByLabelText("Reason"), "she switched to soy");
    await userEvent.click(within(dialog).getByRole("button", { name: "Mark as wrong" }));
    await waitFor(() => expect(api.rejectMemory).toHaveBeenCalledWith(row, "coffee_pref", "she switched to soy"));
  });

  it("shares with every agent and stops sharing, for an admin", async () => {
    await open("admin");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Share with every agent" }));
    await waitFor(() => expect(api.shareMemory).toHaveBeenCalledWith(row, "coffee_pref"));
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "Stop sharing" }));
    await waitFor(() => expect(api.unshareMemory).toHaveBeenCalledWith(identity, "coffee_pref"));
  });

  it("reads the evidence and the links only when their sections open", async () => {
    await open("viewer");
    expect(api.memoryEvidence).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /Evidence/ }));
    expect(await screen.findByText("oat milk")).toBeInTheDocument();
    expect(api.memoryEvidence).toHaveBeenCalledWith(identity, "coffee_pref");
    await userEvent.click(screen.getByRole("button", { name: /Links/ }));
    expect(await screen.findByRole("button", { name: "morning_routine" })).toBeInTheDocument();
    expect(screen.getByText("mentions from")).toBeInTheDocument();
  });

  it("redacts a past version by its exact version, for an admin", async () => {
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
    await waitFor(() => expect(api.redactMemoryVersion).toHaveBeenCalledWith("dev", "m1", "1790000000000000001", ""));
  });

  it("records a writer's verdict on the memory", async () => {
    await open("writer");
    await userEvent.click(within(await menu()).getByRole("menuitem", { name: "This misled" }));
    await waitFor(() => expect(api.recordMemoryOutcome).toHaveBeenCalledWith(row, "coffee_pref", "negative"));
  });
});
