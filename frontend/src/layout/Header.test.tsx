import { act, render, screen, waitFor } from "../test/rtl";
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

// The header's Add adds to the header's namespace: a Library that moves to a collection
// (Back) closes it, and offers no Document while it shows one.
it("closes its Add document when the Library moves to a collection, and offers none there", async () => {
  listMine.mockResolvedValue(jwtOnly);
  useStore.setState({
    libraryElsewhere: false,
    whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] },
  });
  render(<Header />);
  await waitFor(() => expect(screen.getByRole("button", { name: "New" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "New" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: /Document/ }));
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  act(() => useStore.setState({ libraryElsewhere: true }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  await userEvent.click(screen.getByRole("button", { name: "New" }));
  expect(await screen.findByRole("menuitem", { name: /Memory/ })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: /Document/ })).toBeNull();
  useStore.setState({ libraryElsewhere: false });
});

// The New menu is a real menu: arrow keys move between items, Escape closes it and puts
// focus back on the trigger.
it("moves through New's items with the arrow keys and returns focus on Escape", async () => {
  listMine.mockResolvedValue(jwtOnly);
  useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
  const user = userEvent.setup();
  render(<Header />);
  const trigger = screen.getByRole("button", { name: "New" });
  await waitFor(() => expect(trigger).toBeEnabled());
  trigger.focus();
  await user.keyboard("{Enter}");
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  const memory = await screen.findByRole("menuitem", { name: "Memory" });
  await waitFor(() => expect(memory).toHaveFocus());
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "Document" })).toHaveFocus();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  expect(trigger).toHaveFocus();
});

// The account menu says who is signed in through its name: the block at its top holds no
// menu item, so in menu mode a screen reader never reads it.
it("names the account menu after the signed-in subject and org", async () => {
  listMine.mockResolvedValue(jwtOnly);
  useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
  render(<Header />);
  await userEvent.click(screen.getByRole("button", { name: "Account: w@example.com" }));
  const menu = await screen.findByRole("menu", { name: "Signed in as w@example.com, org dev" });
  expect(screen.getByText("Signed in as").closest("[aria-hidden='true']")).not.toBeNull();
  expect(menu).toContainElement(screen.getByRole("menuitem", { name: "Settings" }));
});

// The search field's hint is written the way the keyboard prints it: Ctrl K off Apple
// platforms (the handler takes either modifier). The help button's tooltip names its key.
it("labels the search shortcut for the platform, and the help key in its tooltip", async () => {
  listMine.mockResolvedValue(jwtOnly);
  useStore.setState({ whoami: { authenticated: true, subject: "w@example.com", org: "dev", roles: ["WRITER"] } });
  render(<Header />);
  expect(document.querySelector("#header-search kbd")).toHaveTextContent(/^Ctrl K$/);
  await userEvent.hover(screen.getByRole("button", { name: "Keyboard shortcuts" }));
  expect((await screen.findAllByText("Keyboard shortcuts · ?")).length).toBeGreaterThan(0);
});
