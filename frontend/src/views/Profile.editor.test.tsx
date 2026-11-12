import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { ConfirmViewport } from "../components/ConfirmDialog";
import { Profile } from "./Profile";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";

// The caller's own grant on each namespace the tests use: the role its writes need.
const grant = (namespace: string, role: MyNamespaceEntry["role"] = "writer"): MyNamespaceEntry => ({
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
  subject: "p@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["ns", "other"],
};

// The section editor: Escape drops nothing typed without asking, Tab reaches the text
// before the buttons that save it, and an empty name is an error on its field.

const listProfile = vi.fn();
const getProfileActivity = vi.fn();
const setProfileSection = vi.fn();
const listMine = vi.fn(async (): Promise<MyNamespaceEntry[]> => []);
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listProfile: (...args: unknown[]) => listProfile(...args),
    getProfileActivity: (...args: unknown[]) => getProfileActivity(...args),
    setProfileSection: (...args: unknown[]) => setProfileSection(...args),
    listMine: () => listMine(),
    isAuthDisabled: () => false,
  };
});

async function openNewSection() {
  render(
    <>
      <Profile />
      <ConfirmViewport />
    </>,
  );
  const opener = await screen.findByRole("button", { name: "New section" });
  opener.focus();
  fireEvent.click(opener);
  await screen.findByRole("textbox", { name: "Section text" });
  return opener;
}

describe("Profile: the section editor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listProfile.mockResolvedValue({ entries: [{ label: "persona", value: "Terse.", char_limit: 8192 }] });
    getProfileActivity.mockResolvedValue({ entries: [] });
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: person,
      myNamespaces: [grant("ns"), grant("other")],
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "profile",
    });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null, whoami: null, myNamespaces: null });
  });

  it("holds a request to edit while the role loads, and opens the editor for a writer", async () => {
    useStore.setState({ whoami: null, pendingEditProfileLabel: "persona" });
    render(<Profile />);
    await screen.findByText("persona");
    expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull();
    expect(useStore.getState().pendingEditProfileLabel).toBe("persona");
    act(() => useStore.setState({ whoami: person }));
    expect(await screen.findByRole("textbox", { name: "Section text" })).toHaveValue("Terse.");
    expect(useStore.getState().pendingEditProfileLabel).toBeNull();
  });

  it("spends a request to edit when list_mine could not be read, without opening the editor", async () => {
    listMine.mockRejectedValueOnce(new Error("down"));
    useStore.setState({ myNamespaces: null, pendingEditProfileLabel: "persona" });
    render(<Profile />);
    await screen.findByText("persona");
    await waitFor(() => expect(useStore.getState().pendingEditProfileLabel).toBeNull());
    expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull();
  });

  it("spends a request to edit on a viewer without opening the editor", async () => {
    useStore.setState({ myNamespaces: [grant("ns", "viewer")], pendingEditProfileLabel: "persona" });
    render(<Profile />);
    await screen.findByText("persona");
    await waitFor(() => expect(useStore.getState().pendingEditProfileLabel).toBeNull());
    expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull();
  });

  it("does not reopen an editor from the namespace left on a section of the same name", async () => {
    render(<Profile />);
    fireEvent.click(await screen.findByRole("button", { name: /^Edit/ }));
    await screen.findByRole("textbox", { name: "Section text" });
    // "other" holds a "persona" section too.
    act(() => useStore.setState({ identity: { org: "dev", namespace: "other", agent: "loom-ui" } }));
    await waitFor(() =>
      expect(listProfile.mock.calls.some(([id]) => (id as { namespace: string }).namespace === "other")).toBe(true),
    );
    await screen.findByText("persona");
    expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull();
  });

  it("closes at once on Escape when nothing was typed, and focus returns to the opener", async () => {
    const opener = await openNewSection();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Section text" }), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("asks before Escape drops typed text, and keeps it when the reader declines", async () => {
    await openNewSection();
    const text = screen.getByRole("textbox", { name: "Section text" });
    fireEvent.input(text, { target: { value: "Half a thought" } });
    text.focus();
    fireEvent.keyDown(text, { key: "Escape" });
    const dialog = await screen.findByRole("dialog", { name: "Discard changes?" });
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Section text" })).toHaveValue("Half a thought");
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Section text" })).toHaveFocus());
  });

  it("discards typed text when the reader confirms, and focus returns to the opener", async () => {
    const opener = await openNewSection();
    const text = screen.getByRole("textbox", { name: "Section text" });
    fireEvent.input(text, { target: { value: "Half a thought" } });
    fireEvent.keyDown(text, { key: "Escape" });
    await screen.findByRole("dialog", { name: "Discard changes?" });
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Section text" })).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("puts the text before Cancel and Save in Tab order", async () => {
    await openNewSection();
    const text = screen.getByRole("textbox", { name: "Section text" });
    const cancel = screen.getByRole("button", { name: "Cancel" });
    const save = screen.getByRole("button", { name: "Save" });
    expect(text.compareDocumentPosition(cancel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(text.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says an empty name on the name field, not in a toast", async () => {
    await openNewSection();
    fireEvent.input(screen.getByRole("textbox", { name: "Section text" }), { target: { value: "Some text" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const name = screen.getByRole("textbox", { name: "Section label" });
    await waitFor(() => expect(name).toHaveAttribute("aria-invalid", "true"));
    expect(name).toHaveAccessibleDescription("Label is required");
    expect(setProfileSection).not.toHaveBeenCalled();
  });
});
