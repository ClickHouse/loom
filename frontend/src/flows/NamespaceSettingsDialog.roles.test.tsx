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
    whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["USER"] },
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

  it("tells a token scoped per namespace the role it needs over a direct grant, which caps it", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: [], namespaces: ["team"] },
      myNamespaces: [grant("writer")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue(info);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(NEEDS);
  });

  it("says the server decides for a token scoped per namespace with no grant here", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: [], namespaces: ["team"] },
      myNamespaces: [],
    });
    vi.mocked(api.getNamespace).mockResolvedValue(info);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    const line = "The server decides this token's access to team; it does not show here.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
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
      { org: "dev", namespaces: ["*"], roles: ["WRITER"] },
      "To change its settings, your token must name team and carry an admin claim.",
    ],
    [{ org: "*", namespaces: ["team"], roles: ["ADMIN"] }, "To change its settings, your token must be of dev."],
    [
      { org: "*", namespaces: ["*"], roles: ["VIEWER"] },
      "To change its settings, your token must be of dev, name team and carry an admin claim.",
    ],
    // A token scoped per namespace (no flat roles): its role there does not show here.
    [
      { org: "dev", namespaces: ["team"], roles: [] },
      "The server decides this token's access to team; it does not show here.",
    ],
    [{ org: "dev", namespaces: ["*"], roles: [] }, "To change its settings, your token must name team."],
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

  it.each([[[]], [[grant("viewer")]], [[grant("writer")]]])(
    "asks an admin by claim for a direct admin grant, with grants %j",
    async (rows) => {
      await openRestricted(["ORG_ADMIN"], rows);
      expect(screen.getByTestId("settings-locked")).toHaveTextContent(
        "Changing its settings needs a direct admin grant on team.",
      );
    },
  );

  it.each([
    [
      { org: "*", roles: ["PLATFORM_ADMIN"], namespaces: ["*"] },
      "To change its settings, your token must be of dev and name team, and you need a direct admin grant on team.",
    ],
    [
      { org: "dev", roles: ["ADMIN"], namespaces: ["*"] },
      "To change its settings, your token must name team, and you need a direct admin grant on team.",
    ],
  ])("says both what the token misses and the direct grant, with no grant row: %j", async (token, line) => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", ...token },
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: [],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, classification: "restricted" });
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
  });

  it("tells a writer both what its token misses and the direct grant", async () => {
    await openRestricted(["WRITER"], [grant("writer")]);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(
      "To change its settings, your token must carry an admin claim, and you need a direct admin grant on team.",
    );
  });

  it("tells a `*` writer it must name the namespace and carry an admin claim, and needs the grant", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["WRITER"], namespaces: ["*"] },
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: [grant("writer")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, classification: "restricted" });
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(
      "To change its settings, your token must name team and carry an admin claim, and you need a direct admin grant on team.",
    );
  });

  it("says the role is still loading while list_mine is read", async () => {
    vi.mocked(api.listMine).mockReturnValue(new Promise(() => {}));
    await openRestricted([], null);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent("Your role on this namespace is still loading.");
  });

  it("waits for the grants with an admin claim too, then unlocks for a direct admin", async () => {
    let answer: (rows: MyNamespaceEntry[]) => void = () => {};
    vi.mocked(api.listMine).mockReturnValue(new Promise((r) => (answer = r)));
    await openRestricted(["ADMIN"], null);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent("Your role on this namespace is still loading.");
    await act(async () => answer([grant("admin")]));
    expect(screen.queryByTestId("settings-locked")).toBeNull();
    expect(screen.getByLabelText("Description")).not.toBeDisabled();
  });

  it("says list_mine failed, rather than what the change needs", async () => {
    vi.mocked(api.listMine).mockRejectedValue(new Error("down"));
    await openRestricted([], null);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent("Couldn't check your access to team.");
  });
});

describe("NamespaceSettingsDialog for a repository", () => {
  it("says a change needs a maintainer, naming the repository", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["USER"] },
      myNamespaces: [grant("viewer")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, kind: "code" } as NamespaceInfo);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" label="acme/api @ main" />);
    await act(async () => undefined);
    const line = "Needs maintainer on acme/api @ main. A maintainer of it can grant that.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
  });
});

describe("NamespaceSettingsDialog while the grants are unread", () => {
  const openUnread = async () => {
    // No whoami yet, so nothing settles the role without the grants.
    useStore.setState({
      whoami: null,
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: null,
    });
    vi.mocked(api.getNamespace).mockResolvedValue(info);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
  };

  it("says the role is still loading, on the line and on Delete, never the role it needs", async () => {
    vi.mocked(api.listMine).mockReturnValue(new Promise(() => {}));
    await openUnread();
    const line = "Your role on this namespace is still loading.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
    expect(screen.queryByText(/^Needs admin/)).toBeNull();
  });

  it("still says loading once list_mine answers, while whoami has not", async () => {
    vi.mocked(api.listMine).mockResolvedValue([]);
    await openUnread();
    await act(async () => undefined);
    const line = "Your role on this namespace is still loading.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
  });

  it("names a restricted repository by its label when its grants could not be read", async () => {
    vi.mocked(api.listMine).mockRejectedValue(new Error("down"));
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["ADMIN"], namespaces: ["team"] },
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: null,
    });
    vi.mocked(api.getNamespace).mockResolvedValue({
      ...info,
      kind: "code",
      classification: "restricted",
    } as NamespaceInfo);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" label="acme/api @ main" />);
    await act(async () => undefined);
    await act(async () => undefined);
    const line = "Couldn't check your access to acme/api @ main.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    expect(screen.getByRole("button", { name: "Delete namespace" })).toHaveAccessibleDescription(line);
  });

  it("leaves a signed-in caller's fields open while unread: an unknown grant list is unknown, not no access", async () => {
    vi.mocked(api.listMine).mockReturnValue(new Promise(() => {}));
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["WRITER"] },
      identity: { org: "dev", namespace: "team", agent: "loom-ui" },
      myNamespaces: null,
    });
    vi.mocked(api.getNamespace).mockResolvedValue(info);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    expect(screen.queryByTestId("settings-locked")).toBeNull();
    expect(screen.queryByText(/^Needs admin/)).toBeNull();
  });
});

describe("NamespaceSettingsDialog Delete on a restricted namespace", () => {
  it("is unavailable to an admin by grant whose token the gate refuses, with the same line", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles: ["WRITER"], namespaces: ["team"] },
      myNamespaces: [grant("admin")],
    });
    vi.mocked(api.getNamespace).mockResolvedValue({ ...info, classification: "restricted" });
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    await act(async () => undefined);
    const line = "To change its settings, your token must carry an admin claim.";
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
    const del = screen.getByRole("button", { name: "Delete namespace" });
    expect(del).toHaveAttribute("aria-disabled", "true");
    expect(del).toHaveAccessibleDescription(line);
  });
});

describe("NamespaceSettingsDialog on a restricted repository", () => {
  it.each([
    [["ADMIN"], ["*"], [grant("admin")], "To change its settings, your token must name team."],
    [["ORG_ADMIN"], ["team"], [], "Changing its settings needs a direct maintainer grant on acme/api @ main."],
  ])("names the repository and its maintainer: %j %j", async (roles, namespaces, myNamespaces, line) => {
    useStore.setState({
      whoami: { authenticated: true, subject: "me@example.com", org: "dev", roles, namespaces },
      myNamespaces,
    });
    vi.mocked(api.getNamespace).mockResolvedValue({
      ...info,
      kind: "code",
      classification: "restricted",
    } as NamespaceInfo);
    render(<NamespaceSettingsDialog open onOpenChange={() => {}} org="dev" namespace="team" label="acme/api @ main" />);
    await act(async () => undefined);
    expect(screen.getByTestId("settings-locked")).toHaveTextContent(line);
  });
});
