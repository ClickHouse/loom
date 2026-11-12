/** A member who is not an admin of the namespace sees its settings locked, with the role
 *  that changes them, and the danger zone's Delete unavailable; an admin can delete. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../test/rtl";
import { NamespaceSettingsDialog } from "./NamespaceSettingsDialog";
import { useStore } from "../lib/state";
import * as api from "../api";
import type { MyNamespaceEntry, NamespaceInfo } from "../api";
import { confirmAction } from "../components/ConfirmDialog";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespace: vi.fn() };
});
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => false) }));

const NEEDS = "Needs admin on team. An admin of it can grant that.";

const info: NamespaceInfo = {
  namespace: "team",
  owner_email: "owner@example.com",
  description: "",
  tags: [],
  active: true,
  consolidation_enabled: false,
  conflict_threshold: null,
  daily_cost_cap_usd: 0,
  created_at: "",
  updated_at: "",
};

const grant = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace: "team",
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

async function open(role: MyNamespaceEntry["role"]) {
  useStore.setState({
    whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: [] },
    myNamespaces: [grant(role)],
  });
  vi.mocked(api.getNamespace).mockResolvedValue(info);
  render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
  await act(async () => undefined);
}

beforeEach(() => vi.clearAllMocks());

describe("NamespaceSettingsDialog roles", () => {
  it("locks the fields for a writer, says what changes them, and shows Delete unavailable", async () => {
    await open("writer");
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(NEEDS);
    expect(screen.getByLabelText("Description")).toBeDisabled();
    const del = screen.getByRole("button", { name: "Delete namespace" });
    expect(del).toHaveAttribute("aria-disabled", "true");
    expect(del).toHaveAccessibleDescription(NEEDS);
    fireEvent.click(del);
    expect(confirmAction).not.toHaveBeenCalled();
  });

  it("lets an admin edit and asks before a delete", async () => {
    await open("admin");
    expect(screen.queryByTestId("settings-locked")).toBeNull();
    expect(screen.getByLabelText("Description")).not.toBeDisabled();
    const del = screen.getByRole("button", { name: "Delete namespace" });
    expect(del).not.toHaveAttribute("aria-disabled");
    fireEvent.click(del);
    await act(async () => undefined);
    expect(confirmAction).toHaveBeenCalled();
  });
});
