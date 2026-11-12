import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryDetail, MemoryMoveResult, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { MemoryInspector } from "./MemoryInspector";

// The inspector renames a key through `memory.move` — a dry run first, shown as a
// preview, then the real move, which toasts and re-reads the row. A `status='duplicate'`
// row whose payload carries `canonical_key` wears a "redirects to <key>" badge.

const LIVE: MemoryDetail = {
  found: true,
  value: "oat milk, no sugar",
  memory_type: "preference",
  confidence: 1,
  version: 3,
  pinned: false,
  created_at: "2026-09-18T09:00:00Z",
  updated_at: "2026-09-18T09:00:00Z",
  tags: ["coffee"],
  status: "active",
  agent: "claude-desktop",
};

const REDIRECTED: MemoryDetail = {
  ...LIVE,
  status: "duplicate",
  canonical_key: "prefs/coffee",
};

const MOVED: MemoryMoveResult = {
  from_key: "coffee_pref",
  to_key: "prefs/coffee",
  redirected: true,
  memory_id: "m-2",
  version: 4,
  content_sha256: "abc",
  advisories: [],
  dry_run: false,
  memory_type: "",
  value_preview: "",
};

// The toast store has no viewport in this render; assert the call.
vi.mock("../lib/errorWords", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/errorWords")>()),
  toastCommitted: vi.fn(),
}));
vi.mock("./Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./Toast")>()),
  toast: vi.fn(),
  toastError: vi.fn(),
  ToastViewport: () => null,
}));
vi.mock("./ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true), ConfirmViewport: () => null }));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getMemory: vi.fn(async () => LIVE),
    setMemory: vi.fn(),
    deleteMemory: vi.fn(),
    pinMemory: vi.fn(),
    moveMemory: vi.fn(
      async (_id: unknown, fromKey: string, toKey: string, opts: { dryRun?: boolean; keepRedirect?: boolean } = {}) =>
        opts.dryRun
          ? {
              ...MOVED,
              from_key: fromKey,
              to_key: toKey,
              redirected: false,
              memory_id: "",
              version: 0,
              content_sha256: "",
              dry_run: true,
              memory_type: "preference",
              value_preview: "oat milk, no sugar",
            }
          : { ...MOVED, from_key: fromKey, to_key: toKey, redirected: opts.keepRedirect !== false },
    ),
    memoryHistory: vi.fn(async () => ({ rows: [] })),
    memoryRelated: vi.fn(async () => ({ results: [] })),
    memoryLineage: vi.fn(async () => ({ steps: [] })),
    getProvenance: vi.fn(async () => ({ found: false })),
    listMemories: vi.fn(async () => ({ entries: [], total: 0 })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
  };
});
import { deleteMemory, getMemory, moveMemory, pinMemory, setMemory } from "../api";
import { ApiError } from "../lib/errors";
import { toast } from "./Toast";
import { toastCommitted } from "../lib/errorWords";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
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
  namespaces: ["team-alerts"],
};
const identity = { org: "dev", namespace: "team-alerts", agent: "loom-ui" };

const PENDING = new ApiError({ status: 503, code: "PUBLICATION_PENDING", detail: { retryable: false } });

function seed(role: MyNamespaceEntry["role"]) {
  useStore.setState({
    authed: true,
    whoami: person,
    myNamespaces: [row("team-alerts", role)],
    identity,
    inspectedMemoryKey: "coffee_pref",
  });
}

async function open(onChanged = () => {}) {
  render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={onChanged} />);
  await waitFor(() => expect(getMemory).toHaveBeenCalled());
  await screen.findByText("oat milk, no sugar");
}

/** Rename lives in the footer's More menu. */
async function openRename() {
  await userEvent.click(screen.getByRole("button", { name: "More actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
}

describe("MemoryInspector rename with redirect", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    vi.mocked(getMemory).mockResolvedValue(LIVE);
  });
  afterEach(() => {
    cleanup();
    useStore.setState({
      whoami: null,
      myNamespaces: null,
      identity: null,
      inspectedMemoryKey: null,
      inspectedOwner: null,
    });
  });

  it("previews with dry_run first, then moves for real, toasts and re-reads the row", async () => {
    seed("writer");
    const onChanged = vi.fn();
    await open(onChanged);

    expect(screen.queryByTestId("rename-form")).toBeNull();
    await openRename();
    const form = screen.getByTestId("rename-form");

    const input = within(form).getByLabelText("New key");
    expect(input).toHaveValue("coffee_pref");
    expect(within(form).getByRole("checkbox", { name: "Keep old key readable (redirect)" })).toBeChecked();
    // The prefilled key is the current one — nothing to move yet.
    expect(within(form).getByRole("button", { name: "Preview" })).toBeDisabled();
    expect(within(form).getByRole("button", { name: "Rename" })).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(moveMemory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(moveMemory).mock.calls[0].slice(1)).toEqual([
      "coffee_pref",
      "prefs/coffee",
      { keepRedirect: true, dryRun: true },
    ]);
    const preview = await screen.findByTestId("rename-preview");
    expect(preview).toHaveTextContent(
      "Would write prefs/coffee as preference and leave coffee_pref redirecting to it.",
    );
    expect(preview).toHaveTextContent("oat milk, no sugar");

    vi.mocked(getMemory).mockResolvedValue(REDIRECTED);
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(moveMemory).toHaveBeenCalledTimes(2));
    expect(vi.mocked(moveMemory).mock.calls[1].slice(1)).toEqual([
      "coffee_pref",
      "prefs/coffee",
      { keepRedirect: true, dryRun: false },
    ]);
    // The form closes, the list is told, the row is re-read and now wears the badge.
    await waitFor(() => expect(screen.queryByTestId("rename-form")).toBeNull());
    expect(onChanged).toHaveBeenCalled();
    await waitFor(() => expect(getMemory).toHaveBeenCalledTimes(2));
    const badge = await screen.findByTestId("redirect-badge");
    expect(badge).toHaveTextContent("redirects to prefs/coffee");
    expect(toast).toHaveBeenCalledWith("Renamed to prefs/coffee — the old key redirects to it", "ok");
  });

  it("keeps the form open when the new key is written but the old one is not moved yet", async () => {
    seed("writer");
    const onChanged = vi.fn();
    await open(onChanged);
    await openRename();
    const form = screen.getByTestId("rename-form");
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    vi.mocked(moveMemory).mockRejectedValueOnce(PENDING);
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() =>
      expect(toastCommitted).toHaveBeenCalledWith(
        PENDING,
        "prefs/coffee is written, but coffee_pref is not moved yet. Rename again to finish the move.",
      ),
    );
    // A second Rename finishes the move: the form is still there to send it, and holds the
    // key and the redirect, since another key would leave the first one behind.
    expect(screen.getByTestId("rename-form")).toBeInTheDocument();
    expect(within(form).getByLabelText("New key")).toHaveAttribute("readonly");
    expect(within(form).getByRole("checkbox", { name: "Keep old key readable (redirect)" })).toBeDisabled();
    expect(onChanged).not.toHaveBeenCalled();
    vi.mocked(getMemory).mockResolvedValue(REDIRECTED);
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(screen.queryByTestId("rename-form")).toBeNull());
    expect(vi.mocked(moveMemory).mock.calls.at(-1)?.slice(1, 3)).toEqual(["coffee_pref", "prefs/coffee"]);
    expect(onChanged).toHaveBeenCalled();
  });

  it("closes the editor on a saved value whose search copy lags, and reads the row again", async () => {
    seed("writer");
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const value = screen.getByLabelText("Memory value");
    await userEvent.clear(value);
    await userEvent.type(value, "black");
    vi.mocked(setMemory).mockRejectedValueOnce(PENDING);
    vi.mocked(getMemory).mockResolvedValue({ ...LIVE, value: "black" });
    await userEvent.click(screen.getByRole("button", { name: "Save new version" }));
    await waitFor(() => expect(screen.queryByLabelText("Memory value")).toBeNull());
    expect(await screen.findByText("black")).toBeInTheDocument();
    expect(setMemory).toHaveBeenCalledTimes(1);
  });

  it("holds the redirect on a no_delete row, which a rename without one would delete", async () => {
    seed("writer");
    vi.mocked(getMemory).mockResolvedValue({ ...LIVE, lock_mode: "no_delete" });
    await open();
    await openRename();
    const form = screen.getByTestId("rename-form");
    const redirect = within(form).getByRole("checkbox", { name: "Keep old key readable (redirect)" });
    expect(redirect).toBeChecked();
    expect(redirect).toBeDisabled();
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(moveMemory).toHaveBeenCalled());
    expect(vi.mocked(moveMemory).mock.calls[0][3]).toEqual({ keepRedirect: true, dryRun: false });
  });

  it("a rename that answers after the drawer moved leaves the new key's drawer, and says where the old key went", async () => {
    seed("writer");
    let finish: (r: MemoryMoveResult) => void = () => {};
    vi.mocked(moveMemory).mockImplementationOnce(() => new Promise((r) => (finish = r)));
    const view = render(
      <MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("oat milk, no sugar");
    await openRename();
    const form = screen.getByTestId("rename-form");
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    vi.mocked(getMemory).mockResolvedValue({ ...LIVE, value: "black" });
    view.rerender(
      <MemoryInspector identity={identity} memoryKey="morning_routine" onClose={() => {}} onChanged={() => {}} />,
    );
    await screen.findByText("black");
    await act(async () => finish({ ...MOVED, redirected: false }));
    // The drawer still shows morning_routine, and followMoved did not run: the store still
    // holds the key seed() opened.
    expect(screen.getByText("black")).toBeInTheDocument();
    expect(useStore.getState().inspectedMemoryKey).toBe("coffee_pref");
    expect(toast).toHaveBeenCalledWith("Renamed coffee_pref to prefs/coffee — the old key was deleted", "ok");
  });

  it("a rename cancelled while it runs reads the key again when it answers, under a new Edit", async () => {
    seed("writer");
    let finish: (r: MemoryMoveResult) => void = () => {};
    vi.mocked(moveMemory).mockImplementationOnce(() => new Promise((r) => (finish = r)));
    const onClose = vi.fn();
    render(<MemoryInspector identity={identity} memoryKey="coffee_pref" onClose={onClose} onChanged={() => {}} />);
    await screen.findByText("oat milk, no sugar");
    await openRename();
    const form = screen.getByTestId("rename-form");
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await userEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    vi.mocked(getMemory).mockResolvedValue({ found: false } as never);
    await act(async () => finish({ ...MOVED, redirected: false }));
    // The key was deleted: the drawer reads it again and shows it gone, with no form left
    // open behind it, so one Escape closes the drawer.
    expect(await screen.findByText(/not found/i)).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith("Renamed coffee_pref to prefs/coffee — the old key was deleted", "ok");
    // The shortcuts have no row to act on, so none of them writes the deleted key back.
    await userEvent.keyboard("edp{Meta>}{Enter}{/Meta}");
    expect(setMemory).not.toHaveBeenCalled();
    expect(deleteMemory).not.toHaveBeenCalled();
    expect(pinMemory).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("unticking the redirect sends keep_redirect false and follows the new key", async () => {
    seed("admin");
    await open();
    await openRename();
    const form = screen.getByTestId("rename-form");
    await userEvent.click(within(form).getByRole("checkbox", { name: "Keep old key readable (redirect)" }));
    expect(form).toHaveTextContent("The old key is deleted");
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(moveMemory).toHaveBeenCalledTimes(1));
    expect(vi.mocked(moveMemory).mock.calls[0][3]).toEqual({ keepRedirect: false, dryRun: false });
    await waitFor(() => expect(useStore.getState().inspectedMemoryKey).toBe("prefs/coffee"));
  });

  it("a duplicate row with canonical_key wears the redirect badge; clicking it opens the canonical", async () => {
    vi.mocked(getMemory).mockResolvedValue(REDIRECTED);
    seed("viewer");
    await open();
    const badge = screen.getByTestId("redirect-badge");
    expect(badge).toHaveTextContent("redirects to prefs/coffee");
    expect(badge).toHaveAccessibleName("Redirects to prefs/coffee; open it");
    await userEvent.click(badge);
    expect(useStore.getState().inspectedMemoryKey).toBe("prefs/coffee");
  });

  it("a renamed row opened under its user keeps that writer; a redirect's target opens as any row", async () => {
    const owned = { ...identity, agent: "claude-code", user_id: "ada", session_id: "-" };
    seed("admin");
    render(<MemoryInspector identity={owned} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("oat milk, no sugar");
    await openRename();
    const form = screen.getByTestId("rename-form");
    await userEvent.click(within(form).getByRole("checkbox", { name: "Keep old key readable (redirect)" }));
    const input = within(form).getByLabelText("New key");
    await userEvent.clear(input);
    await userEvent.type(input, "prefs/coffee");
    await userEvent.click(within(form).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(useStore.getState().inspectedMemoryKey).toBe("prefs/coffee"));
    expect(useStore.getState()).toMatchObject({
      inspectedAgent: "claude-code",
      inspectedOwner: { user_id: "ada", session_id: "-" },
    });
    cleanup();

    vi.mocked(getMemory).mockResolvedValue(REDIRECTED);
    render(<MemoryInspector identity={owned} memoryKey="coffee_pref" onClose={() => {}} onChanged={() => {}} />);
    await userEvent.click(await screen.findByTestId("redirect-badge"));
    expect(useStore.getState()).toMatchObject({ inspectedMemoryKey: "prefs/coffee", inspectedOwner: null });
  });

  it("a live row has no badge, and a viewer's rename is unavailable", async () => {
    seed("viewer");
    await open();
    expect(screen.queryByTestId("redirect-badge")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    await screen.findByRole("menuitem", { name: "Copy" });
    const rename = screen.getByRole("menuitem", { name: "Rename" });
    expect(rename).toHaveAccessibleDescription("Needs writer on team-alerts. An admin of it can grant that.");
    await userEvent.click(rename);
    expect(screen.queryByTestId("rename-form")).toBeNull();
    expect(moveMemory).not.toHaveBeenCalled();
  });
});
