import { cleanup, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { ProfileSectionDialog } from "./ProfileSectionDialog";

// The section dialog's Edit hands the section to the Profile page's editor: profile.set
// (WRITER), so below that it stays in view, unavailable with why, and hands nothing over.

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listProfile: vi.fn(async () => ({ entries: [{ label: "bio", value: "Terse.", char_limit: 8192 }] })),
  listMine: vi.fn(async () => []),
}));

const grant = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: "ns",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

function seed(role: MyNamespaceEntry["role"]) {
  resetNamespaceRoleCache();
  useStore.setState({
    whoami: { authenticated: true, subject: "p@example.com", org: "dev", roles: ["USER"], namespaces: ["ns"] },
    myNamespaces: [grant(role)],
    identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
    inspectedProfileLabel: "bio",
    pendingEditProfileLabel: null,
  });
}

describe("ProfileSectionDialog's Edit", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedProfileLabel: null });
  });

  it("is unavailable to a viewer, with what it needs, and asks for no edit", async () => {
    seed("viewer");
    render(<ProfileSectionDialog />);
    await screen.findByText("Terse.");
    const edit = screen.getByRole("button", { name: "Edit" });
    expect(edit).toHaveAttribute("aria-disabled", "true");
    expect(edit).toHaveAccessibleDescription("Needs writer on ns. An admin of it can grant that.");
    await userEvent.click(edit);
    expect(useStore.getState().pendingEditProfileLabel).toBeNull();
  });

  it("asks the Profile page to edit the section for a writer", async () => {
    seed("writer");
    render(<ProfileSectionDialog />);
    await screen.findByText("Terse.");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(useStore.getState().pendingEditProfileLabel).toBe("bio");
  });
});
