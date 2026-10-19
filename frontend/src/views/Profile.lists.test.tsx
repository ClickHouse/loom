import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import { useStore } from "../lib/state";
import { Profile } from "./Profile";

// The profile's sections as a list: rows on ListRow, the one inline
// action (Edit) and a More menu for the rest, the toolbar in every state with its count
// and the selection's actions, select-all and a Shift-click range, Escape clears, and a
// failed read said with a Retry.

const api = vi.hoisted(() => ({
  listProfile: vi.fn(),
  getProfileActivity: vi.fn(async () => ({ entries: [] })),
  deleteProfileSection: vi.fn(async () => ({})),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  ...api,
  isAuthDisabled: () => false,
}));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

const section = (label: string, value: string, over: Record<string, unknown> = {}) => ({
  label,
  value,
  char_limit: 8192,
  content_sha256: "x",
  version: 1,
  created_at: "2026-09-20T00:00:00Z",
  updated_at: "2026-09-27T12:00:00Z",
  last_edited_by: "loom-ui",
  ...over,
});
const SECTIONS = [
  section("about-me", "I run the payments on-call."),
  section("persona", "Terse.\nNo preamble.\nPush back."),
  section("tools", "x".repeat(7900)),
];

const toolbar = () => screen.getByLabelText("Search and filters");

describe("Profile sections list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listProfile.mockResolvedValue({ entries: SECTIONS });
    useStore.setState({ authed: true, identity: { org: "dev", namespace: "ns", agent: "loom-ui" }, view: "profile" });
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ identity: null });
  });

  it("draws each section as a list row: label, size, who and when, Edit inline, the rest in More", async () => {
    render(<Profile />);
    await screen.findByText("about-me");
    expect(toolbar()).toHaveTextContent("1–3 of 3");
    const row = screen.getByText("about-me").closest(".list-row") as HTMLElement;
    expect(row.querySelector("[data-row-open]")).not.toBeNull();
    expect(row.querySelector("time")).not.toBeNull();
    expect(within(row).getByRole("button", { name: "Edit about-me" })).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Actions for about-me" }));
    const menu = await screen.findByRole("menu", { name: "Actions for about-me" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Copy text", "Delete"]);
    // A section at 95% of its cap says so with a warning badge.
    const full = screen.getByText("tools").closest(".list-row") as HTMLElement;
    expect(within(full).getByText("Near limit")).toBeInTheDocument();
  });

  it("opens a section from its row", async () => {
    render(<Profile />);
    await screen.findByText("persona");
    await userEvent.click(screen.getByRole("button", { name: /^persona/ }));
    expect(useStore.getState().inspectedProfileLabel).toBe("persona");
  });

  it("keeps the toolbar up while loading, and says a failed read with a Retry", async () => {
    api.listProfile
      .mockRejectedValueOnce(new ApiError({ status: 503, message: "ClickHouse timed out", requestId: "rid-3" }))
      .mockResolvedValueOnce({ entries: SECTIONS });
    render(<Profile />);
    expect(screen.getByLabelText("Search profile sections")).toBeInTheDocument();
    expect(await screen.findByText("Couldn't load profile sections")).toBeInTheDocument();
    expect(screen.getByText("rid-3").tagName).toBe("CODE");
    expect(toolbar()).not.toHaveTextContent(/\d/);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("about-me")).toBeInTheDocument();
  });

  it("filters to the matches, and says when none match", async () => {
    render(<Profile />);
    await screen.findByText("about-me");
    await userEvent.type(screen.getByLabelText("Search profile sections"), "payments");
    expect(toolbar()).toHaveTextContent("1 match");
    expect(screen.queryByText("persona")).toBeNull();
    await userEvent.type(screen.getByLabelText("Search profile sections"), "zzz");
    expect(
      await screen.findByText((_, el) => el?.textContent === "No profile sections match paymentszzz"),
    ).toBeVisible();
  });

  it("selects a Shift-click range and every section, deletes them, and Escape clears", async () => {
    render(<Profile />);
    await screen.findByText("about-me");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox", { name: "Select section about-me" }));
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("checkbox", { name: "Select section tools" }));
    await user.keyboard("{/Shift}");
    expect(toolbar()).toHaveTextContent("3 selected");
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard("{Escape}");
    expect(toolbar()).not.toHaveTextContent("selected");

    await user.click(screen.getByRole("checkbox", { name: "Select section persona" }));
    await user.click(within(toolbar()).getByRole("checkbox", { name: "Select all 3 on this page" }));
    expect(toolbar()).toHaveTextContent("3 selected");
    await user.click(within(toolbar()).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.deleteProfileSection).toHaveBeenCalledTimes(3));
  });
});
