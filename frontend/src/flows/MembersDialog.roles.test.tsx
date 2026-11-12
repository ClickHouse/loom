/** A member who is not an admin of the namespace sees the admin's controls, unavailable,
 *  with the role they need; an admin uses them. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { useStore } from "../lib/state";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listGrants: vi.fn(),
    approveGrant: vi.fn(async () => ({})),
    revokeGrant: vi.fn(async () => ({})),
  };
});

const NEEDS = "Needs admin on team. An admin of it can grant that.";
const at = "2026-09-17T14:50:28Z";
const ROWS = [
  { subject: "bob", role: "viewer", status: "pending", pending_since: at, granted_at: at, granted_by: "bob" },
  { subject: "carol", role: "writer", status: "active", granted_at: at, granted_by: "alice" },
  { subject: "viewer-me", role: "viewer", status: "active", granted_at: at, granted_by: "alice" },
];

const signIn = (subject: string, roles: string[]) =>
  useStore.setState({ whoami: { authenticated: true, subject, org: "dev", roles } });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listGrants).mockResolvedValue({ rows: ROWS } as never);
});

describe("MembersDialog for a member who is not an admin", () => {
  it("shows Approve, Reject, Remove and Add member unavailable, with the role they need, and sends nothing", async () => {
    signIn("viewer-me", ["VIEWER"]);
    render(<MembersDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    const approve = await screen.findByRole("button", { name: "Approve as viewer" });
    for (const name of ["Approve as viewer", "Reject", "Remove carol", "Add member"]) {
      const control = screen.getByRole("button", { name });
      expect(control).toHaveAttribute("aria-disabled", "true");
      expect(control).toHaveAccessibleDescription(NEEDS);
    }
    // The member's role stays as text; nobody's own row offers a Remove.
    expect(screen.queryByRole("combobox", { name: "Role for carol" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove viewer-me" })).toBeNull();
    fireEvent.click(approve);
    fireEvent.click(screen.getByRole("button", { name: "Remove carol" }));
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    expect(api.approveGrant).not.toHaveBeenCalled();
    expect(api.revokeGrant).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/member to add/)).toBeNull();
  });

  it("still lets an admin approve and add", async () => {
    signIn("alice", ["ADMIN"]);
    render(<MembersDialog open onOpenChange={() => {}} org="dev" namespace="team" />);
    const approve = await screen.findByRole("button", { name: "Approve as viewer" });
    expect(approve).not.toHaveAttribute("aria-disabled");
    fireEvent.click(approve);
    await waitFor(() => expect(api.approveGrant).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));
    expect(await screen.findByLabelText(/member to add/)).toBeInTheDocument();
  });
});

describe("MembersDialog for a repository", () => {
  it("tells a reader each control needs a maintainer, naming the repository", async () => {
    signIn("viewer-me", ["VIEWER"]);
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="dev"
        namespace="code-acme-api-main"
        kind="code"
        label="acme/api @ main"
      />,
    );
    await screen.findByRole("button", { name: /^Approve as/ });
    const line = "Needs maintainer on acme/api @ main. A maintainer of it can grant that.";
    for (const name of [/^Approve as/, /^Reject$/, /^Remove carol$/, /^Add reader$/])
      expect(screen.getByRole("button", { name })).toHaveAccessibleDescription(line);
  });
});
