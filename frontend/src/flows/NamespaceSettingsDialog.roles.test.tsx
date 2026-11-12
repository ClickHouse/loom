/** A member who is not an admin of the namespace sees its settings locked, with the role
 *  that changes them, and the danger zone's Delete unavailable; an admin can delete. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "../test/rtl";
import { NamespaceSettingsDialog } from "./NamespaceSettingsDialog";
import { useStore } from "../lib/state";
import * as api from "../api";
import type { MyNamespaceEntry, NamespaceInfo } from "../api";
import { confirmAction } from "../components/ConfirmDialog";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespace: vi.fn(), listMine: vi.fn() };
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

beforeEach(() => {
  vi.clearAllMocks();
  resetNamespaceRoleCache();
});

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

describe("NamespaceSettingsDialog on a restricted namespace", () => {
  it.each([
    [
      { org: "dev", namespaces: ["*"], roles: [] },
      "To change its settings, your token must name team and carry an admin claim.",
    ],
    [{ org: "*", namespaces: ["team"], roles: ["ADMIN"] }, "To change its settings, your token must be of dev."],
    [
      { org: "*", namespaces: ["*"], roles: [] },
      "To change its settings, your token must be of dev, name team and carry an admin claim.",
    ],
  ])("tells an admin by grant what the token misses: %j", async (token, line) => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", ...token },
      myNamespaces: [grant("admin")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, classification: "restricted" });
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByLabelText("Description")).toBeDisabled();
  });
});

describe("NamespaceSettingsDialog on a restricted namespace, before and beside the token", () => {
  const openRestricted = async (roles: string[], myNamespaces: MyNamespaceEntry[] | null) => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles, namespaces: ["team"] },
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces,
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, classification: "restricted" });
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
  };

  it("asks for a direct admin grant when the caller's is inherited", async () => {
    await openRestricted(["ADMIN"], [{ ...grant("admin"), inherited: true }]);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(
      "Changing its settings needs a direct admin grant on team.",
    );
  });

  it("says the role is still loading while list_mine is read", async () => {
    vi.mocked(api.listMine).mockReturnValue(new Promise(() => {}));
    await openRestricted([], null);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent("Your role on this namespace is still loading.");
  });

  it("says list_mine failed, rather than what the change needs", async () => {
    vi.mocked(api.listMine).mockRejectedValue(new Error("down"));
    await openRestricted([], null);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent("Couldn't check your access to team.");
  });
});

describe("NamespaceSettingsDialog for a repository", () => {
  it("says a change needs a maintainer", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: [] },
      myNamespaces: [grant("viewer")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, kind: "code" } as NamespaceInfo);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    const line = "Needs maintainer on team. A maintainer of it can grant that.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
  });
});
