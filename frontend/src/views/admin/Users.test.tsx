import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { listCollections, type AdminUserRow, type AdminUsersResult } from "../../api";
import { invalidateShared } from "../../lib/sharedReads";
import { useStore } from "../../lib/state";
import { Users } from "./Users";
import { ApiError } from "../../lib/errors";

// admin.users has no offset: the page reads up to 1,000 accounts once and pages them in
// Click UI's Table, 50 at a time. A filter starts again from the first page; the first
// read is the Table's header over a skeleton.

const adminListUsers = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    adminListUsers: (...a: unknown[]) => adminListUsers(...a),
    listCollections: vi.fn(async () => ({
      collections: [{ namespace: "col-r", parent: "", personal: false, role: "admin", title: "Research" }],
      next_cursor: null,
    })),
  };
});

const user = (i: number, pending = false): AdminUserRow => ({
  subject: `user-${i}@example.com`,
  contact_email: "",
  grants: [
    {
      namespace: "team",
      role: "viewer",
      is_owner: false,
      granted_at: null,
      ...(pending ? { pending_role: "writer" } : {}),
    },
  ],
  is_platform_admin: false,
  last_active: null,
  has_password_auth: true,
});
const result = (users: AdminUserRow[]): AdminUsersResult => ({
  users,
  total: users.length,
  total_pending_elevations: users.filter((u) => u.grants.some((g) => g.pending_role)).length,
  total_active_last_7d: 0,
  platform_admins_configured: [],
});

const bodyRows = () =>
  within(screen.getByRole("table", { name: "Users" }))
    .getAllByRole("row")
    .slice(1);

beforeEach(() => {
  adminListUsers.mockReset();
  localStorage.clear();
  useStore.setState({
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    whoami: { authenticated: true, subject: "op@example.com", org: "acme", roles: ["org_admin"], namespaces: [] },
  });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null, whoami: null, myNamespaces: null });
  invalidateShared();
});

describe("Users table", () => {
  // The count's live region is there before the first read answers, so the count is said.
  it("mounts its count region before the read answers", async () => {
    adminListUsers.mockImplementation(() => new Promise((r) => setTimeout(() => r(result([user(1), user(2)])), 50)));
    render(<Users />);
    const bar = document.querySelector<HTMLElement>('[role="group"][aria-label="Filters and count"]');
    const live = bar?.querySelector('[aria-live="polite"]');
    expect(live).toBeEmptyDOMElement();
    await waitFor(() => expect(live).toHaveTextContent("1–2 of 2"));
  });

  it("reads up to 1,000 accounts and shows them 50 to a page", async () => {
    adminListUsers.mockResolvedValue(result(Array.from({ length: 120 }, (_, i) => user(i + 1))));
    render(<Users />);
    await screen.findByText("user-1@example.com");
    expect(adminListUsers.mock.calls[0][0].limit).toBe(1000);
    expect(bodyRows()).toHaveLength(50);
    expect(screen.getByRole("navigation", { name: "Users pages" })).toHaveTextContent("1–50 of 120");

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("user-51@example.com")).toBeInTheDocument();
    expect(screen.queryByText("user-1@example.com")).toBeNull();
    expect(document.getElementById("users-list")).toHaveFocus();
  });

  it("starts a filter from its first page, and draws no pager for one page", async () => {
    const users = Array.from({ length: 120 }, (_, i) => user(i + 1, i < 3));
    adminListUsers.mockResolvedValue(result(users));
    render(<Users />);
    await screen.findByText("user-1@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await screen.findByText("user-51@example.com");
    fireEvent.click(screen.getByRole("button", { name: /Pending elevation/ }));
    expect(await screen.findByText("user-1@example.com")).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(3);
    expect(screen.queryByRole("navigation", { name: "Users pages" })).toBeNull();
  });

  it("draws the first read as the table's header over a skeleton, marked busy", async () => {
    adminListUsers.mockReturnValue(new Promise(() => {}));
    render(<Users />);
    const table = await screen.findByRole("table", { name: "Users" });
    expect(table).toHaveAttribute("aria-busy", "true");
    // The first read waits 300 ms before its loader shows, like every loader
    // (ui/LoadingSkeleton), under the header row already in place.
    expect(within(table).queryByText("Loading data")).toBeNull();
    expect(within(table).getByRole("status", { name: "Loading users" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Subject" })).toBeInTheDocument();
  });
});

const toolbar = () => document.querySelector<HTMLElement>('[role="group"][aria-label="Filters and count"]');

describe("Users count, times and failure", () => {
  it("says the list holds the first 1,000 accounts when the org has more", async () => {
    adminListUsers.mockResolvedValue({
      ...result(Array.from({ length: 1000 }, (_, i) => user(i + 1))),
      total: 3421,
    });
    render(<Users />);
    await screen.findByText("user-1@example.com");
    expect(toolbar()).toHaveTextContent("the first 1,000 of 3,421 accounts");
    expect(screen.getByRole("button", { name: /All/ })).toHaveTextContent("All1,000");
  });

  it("counts the page in the toolbar, once", async () => {
    adminListUsers.mockResolvedValue(result(Array.from({ length: 120 }, (_, i) => user(i + 1))));
    render(<Users />);
    await screen.findByText("user-1@example.com");
    expect(toolbar()).toHaveTextContent("1–50 of 120");
    expect(screen.queryByText(/accounts? shown/)).toBeNull();
    // The All filter keeps its count.
    expect(screen.getByRole("button", { name: /All/ })).toHaveTextContent("All120");
  });

  it("shows last activity as a list time", async () => {
    const at = new Date(Date.now() - 3 * 3600_000).toISOString();
    adminListUsers.mockResolvedValue(result([{ ...user(1), last_active: at }]));
    render(<Users />);
    const time = await screen.findByText("3h ago");
    expect(time.tagName).toBe("TIME");
  });

  it("says a failed read failed, with Retry, and draws no filters or count", async () => {
    adminListUsers.mockRejectedValueOnce(new ApiError({ status: 503, message: "down", requestId: "req-7" }));
    render(<Users />);
    expect(await screen.findByText("Couldn't load users")).toBeInTheDocument();
    expect(screen.getByText(/req-7/)).toBeInTheDocument();
    expect(screen.queryByText("No accounts yet")).toBeNull();
    adminListUsers.mockResolvedValue(result([user(1)]));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("user-1@example.com")).toBeInTheDocument();
  });
});

describe("Users paging while a read is in flight", () => {
  it("offers no page of the old accounts while the service-accounts read is in flight", async () => {
    adminListUsers.mockResolvedValueOnce(result(Array.from({ length: 120 }, (_, i) => user(i + 1))));
    render(<Users />);
    await screen.findByText("user-1@example.com");
    let answer: (v: unknown) => void = () => {};
    adminListUsers.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(screen.getByRole("checkbox", { name: "Include service accounts" }));
    await waitFor(() => expect(adminListUsers).toHaveBeenCalledTimes(2));
    // The accounts on screen were read without service accounts: no pager pages them.
    expect(screen.queryByRole("navigation", { name: "Users pages" })).toBeNull();
    await act(async () => answer(result(Array.from({ length: 130 }, (_, i) => user(i + 1)))));
    expect(await screen.findByText("user-1@example.com")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Users pages" })).toHaveTextContent("1–50 of 130");
  });
});

// A read started by Refresh or Retry is dropped when a newer read overtakes it, so an answer
// for the set the page has left (service accounts in or out) never lands after the new one's.
describe("Users late answers", () => {
  it("drops a Refresh's answer that lands after the service-accounts toggle's", async () => {
    adminListUsers.mockResolvedValueOnce(result([user(1)]));
    render(<Users />);
    await screen.findByText("user-1@example.com");
    let late: (v: unknown) => void = () => {};
    adminListUsers.mockReturnValueOnce(new Promise((r) => (late = r)));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    adminListUsers.mockResolvedValueOnce(result([user(2)]));
    fireEvent.click(screen.getByRole("checkbox", { name: "Include service accounts" }));
    expect(await screen.findByText("user-2@example.com")).toBeInTheDocument();
    await act(async () => late(result([user(3)])));
    expect(screen.queryByText("user-3@example.com")).toBeNull();
    expect(screen.getByText("user-2@example.com")).toBeInTheDocument();
  });
});

describe("Users, the service-accounts toggle", () => {
  it("keeps the toolbar and draws neither the old rows nor their count until its read answers", async () => {
    adminListUsers.mockResolvedValueOnce(result([user(1), user(2)])).mockReturnValueOnce(new Promise(() => {}));
    render(<Users />);
    expect(await screen.findAllByRole("row")).not.toHaveLength(0);
    await waitFor(() => expect(bodyRows()).toHaveLength(2));
    fireEvent.click(screen.getByRole("checkbox", { name: "Include service accounts" }));
    await waitFor(() => expect(adminListUsers).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("checkbox", { name: "Include service accounts" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Filters and count" })).not.toHaveTextContent(/of 2/);
    expect(screen.getByRole("table", { name: "Users" })).toHaveAttribute("aria-busy", "true");
  });

  it("tags a grant on a collection as a collection, by its title", async () => {
    const u = user(1);
    u.grants = [
      ...u.grants,
      { namespace: "col-r", role: "writer", is_owner: false, granted_at: null },
      { namespace: "col-hidden", role: "viewer", is_owner: false, granted_at: null },
    ];
    adminListUsers.mockResolvedValue(result([u]));
    render(<Users />);
    expect(await screen.findByText(/collection Research · writer/)).toBeInTheDocument();
    // One the viewer cannot read keeps its name, still tagged.
    expect(screen.getByText(/collection col-hidden · viewer/)).toBeInTheDocument();
    expect(screen.getByText(/team · viewer/)).toBeInTheDocument();
    // Titles repeat: the collection's place and address are in its tip.
    const tag = screen.getByText(/collection Research · writer/).closest('[role="note"]') as HTMLElement;
    tag.focus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Library · col-r");
    // One the viewer cannot read: its place is unknown, so the tip claims none.
    tag.blur();
    const hidden = screen.getByText(/collection col-hidden · viewer/).closest('[role="note"]') as HTMLElement;
    hidden.focus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(/^col-hidden$/));
  });

  it("names another person's own collection as a personal collection, never as the admin's My files", async () => {
    const u = user(1);
    u.grants = [...u.grants, { namespace: "col-me-bob", role: "admin", is_owner: true, granted_at: null }];
    adminListUsers.mockResolvedValue(result([u]));
    render(<Users />);
    expect(await screen.findByText(/^personal collection · admin · owner$/)).toBeInTheDocument();
    expect(screen.queryByText(/My files/)).toBeNull();
  });

  it("tells two of one person's collection grants that look alike apart by name", async () => {
    const u = user(1);
    u.grants = [
      ...u.grants,
      { namespace: "col-a", role: "viewer", is_owner: false, granted_at: null },
      { namespace: "col-b", role: "viewer", is_owner: false, granted_at: null },
    ];
    adminListUsers.mockResolvedValue(result([u]));
    invalidateShared();
    vi.mocked(listCollections).mockResolvedValueOnce({
      collections: [
        { namespace: "col-a", parent: "", personal: false, role: "admin", title: "Research" },
        { namespace: "col-b", parent: "", personal: false, role: "admin", title: "Research" },
      ],
      next_cursor: null,
    });
    render(<Users />);
    expect(await screen.findByText(/^collection Research \(col-a\) · viewer$/)).toBeInTheDocument();
    expect(screen.getByText(/^collection Research \(col-b\) · viewer$/)).toBeInTheDocument();
  });

  it("names a collection past the list's first page by the title the viewer's own row carries", async () => {
    const u = user(1);
    u.grants = [...u.grants, { namespace: "col-z", role: "viewer", is_owner: false, granted_at: null }];
    adminListUsers.mockResolvedValue(result([u]));
    useStore.setState({
      myNamespaces: [
        {
          namespace: "col-z",
          role: "admin",
          status: "active",
          visibility: "private",
          description: "",
          joined_at: null,
          is_new: false,
          kind: "collection",
          title: "Zeta",
        },
      ],
    });
    render(<Users />);
    expect(await screen.findByText(/^collection Zeta · viewer$/)).toBeInTheDocument();
  });

  it("names a personal collection by its address until the collection read says whose it is", async () => {
    const u = user(1);
    u.grants = [...u.grants, { namespace: "col-me-op", role: "admin", is_owner: true, granted_at: null }];
    adminListUsers.mockResolvedValue(result([u]));
    invalidateShared();
    vi.mocked(listCollections).mockReturnValueOnce(new Promise(() => {}));
    render(<Users />);
    // It could be the viewer's own (My files): neither "personal collection" nor a guess.
    expect(await screen.findByText(/^collection col-me-op · admin · owner$/)).toBeInTheDocument();
    expect(screen.queryByText(/personal collection/)).toBeNull();
  });

  it("keeps the collection tag on a shared collection whose title reads like a personal one", async () => {
    const u = user(1);
    u.grants = [...u.grants, { namespace: "col-p", role: "viewer", is_owner: false, granted_at: null }];
    adminListUsers.mockResolvedValue(result([u]));
    invalidateShared();
    vi.mocked(listCollections).mockResolvedValueOnce({
      collections: [{ namespace: "col-p", parent: "", personal: false, role: "admin", title: "personal collection" }],
      next_cursor: null,
    });
    render(<Users />);
    expect(await screen.findByText(/^collection personal collection · viewer$/)).toBeInTheDocument();
  });
});
