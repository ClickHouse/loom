import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Header } from "./Header";

// The header says what the role lets you do. A viewer on a claim-only namespace gets a
// disabled "+ New" that names the reason, not a live one the server would refuse. The
// phone header carries a search button that reveals the palette input.

const listMine = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: (...args: unknown[]) => listMine(...args),
    listMemories: vi.fn(async () => ({ entries: [], truncated: false, total: 0 })),
    listProfile: vi.fn(async () => ({ entries: [] })),
    browseNamespaces: vi.fn(async () => []),
    listGrants: vi.fn(async () => ({ rows: [] })),
    listPendingApprovals: vi.fn(async () => ({ rows: [] })),
  };
});

const identity = { org: "dev", namespace: "reporter-public", agent: "dashboard" };
const jwtOnly: MyNamespaceEntry[] = [
  {
    namespace: "reporter-public",
    role: "jwt",
    status: "active",
    visibility: "public",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  },
];

beforeEach(() => {
  resetNamespaceRoleCache();
  listMine.mockReset();
  useStore.setState({ identity, view: "memories", myNamespaces: null });
});

describe("Header", () => {
  it("disables New for a viewer token on a claim-only namespace, and says why", async () => {
    listMine.mockResolvedValue(jwtOnly);
    useStore.setState({ whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] } });
    render(<Header />);
    // The live button is replaced by the disabled one once list_mine resolves.
    // aria-disabled, not disabled: the trigger stays focusable so its tooltip can say why.
    await waitFor(() => expect(screen.getByRole("button", { name: "New" })).toHaveAttribute("aria-disabled", "true"));
    expect(screen.getByRole("button", { name: "New" })).toHaveAccessibleDescription(/You have read-only access here/);
  });

  it("keeps New live for a writer token on the same namespace", async () => {
    listMine.mockResolvedValue(jwtOnly);
    useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
    render(<Header />);
    await waitFor(() => expect(listMine).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "New" })).toBeEnabled();
  });

  it("reads the 8540 store slice first and skips the list_mine read", async () => {
    useStore.setState({
      whoami: { authenticated: true, subject: "v@example.com", org: "dev", roles: ["VIEWER"] },
      myNamespaces: jwtOnly,
    });
    render(<Header />);
    await waitFor(() => expect(screen.getByRole("button", { name: "New" })).toHaveAttribute("aria-disabled", "true"));
    // PendingApprovalsBell reads list_mine on its own; the role hook must not add a call.
    expect(listMine.mock.calls.length).toBeLessThanOrEqual(1);
  });

  it("never locks New while the role is unknown", async () => {
    listMine.mockRejectedValue(new Error("offline"));
    useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
    render(<Header />);
    await waitFor(() => expect(listMine).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "New" })).toBeEnabled();
  });

  it("has a Search button that reveals the palette input", async () => {
    listMine.mockResolvedValue(jwtOnly);
    useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
    render(<Header />);
    const search = screen.getByRole("button", { name: "Search" });
    expect(search).toHaveAttribute("aria-expanded", "false");
    await userEvent.setup().click(search);
    expect(search).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Search Loom")).toHaveFocus();
  });
});

// A dialog is mounted while it is open — closed, AddMemoryDialog/AddDocumentDialog ran
// their hooks and element trees on every page's boot (42 ms of main thread at 4x CPU).
it("mounts no dialog while closed, and one when New → Document is chosen", async () => {
  listMine.mockResolvedValue(jwtOnly);
  useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
  render(<Header />);
  await waitFor(() => expect(screen.getByRole("button", { name: "New" })).toBeEnabled());
  expect(screen.queryByRole("dialog")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "New" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: /Document/ }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
});
