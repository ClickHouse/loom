import { cleanup, fireEvent, render, screen, within } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AdminUserRow, AdminUsersResult } from "../../api";
import { useStore } from "../../lib/state";
import { Users } from "./Users";

// admin.users has no offset: the page reads up to 1,000 accounts once and pages them in
// Click UI's Table, 50 at a time. A filter starts again from the first page; the first
// read is the Table's own loading row.

const adminListUsers = vi.fn();
vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, adminListUsers: (...a: unknown[]) => adminListUsers(...a) };
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
  useStore.setState({ identity: null, whoami: null });
});

describe("Users table", () => {
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

  it("draws the first read as Click UI's loading table, marked busy", async () => {
    adminListUsers.mockReturnValue(new Promise(() => {}));
    render(<Users />);
    const table = await screen.findByRole("table", { name: "Users" });
    expect(table).toHaveAttribute("aria-busy", "true");
    expect(within(table).getByText("Loading data")).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Subject" })).toBeInTheDocument();
  });
});
