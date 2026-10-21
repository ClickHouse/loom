import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowseEntry, MyNamespaceEntry } from "../api";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { Browse } from "./Browse";

// A space is one list row: the namespace in mono and its visibility badge
// on line 1, its description on line 2; at the right its member count, the one inline
// action (Invite where you manage it, else Join, Pending or Request upgrade) and a More
// menu (Settings for a manager, Copy name, Leave for a member). The rows are one tab stop
// (`useRowNav`); Create namespace is the page header's primary.

const browse: BrowseEntry[] = [
  {
    namespace: "alpha",
    description: "Support team runbooks",
    guide_revision: 2,
    guide_summary: "One runbook per service, keyed runbook/<service>",
    kind: "memory",
    owner_email: "",
    member_count: 1,
    created_at: "",
  },
  {
    namespace: "beta",
    description: "",
    kind: "memory",
    owner_email: "",
    member_count: 2,
    created_at: "",
    guide_revision: 1,
    guide_summary: "Beta's conventions",
  },
];
const mine: MyNamespaceEntry[] = [
  {
    namespace: "team",
    role: "admin",
    status: "active",
    visibility: "private",
    description: "The platform team",
    joined_at: null,
    is_new: false,
    kind: "memory",
  },
  {
    namespace: "beta",
    role: "viewer",
    status: "active",
    visibility: "public",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  },
];

const leaveNamespace = vi.fn(async () => ({}));
const joinNamespace = vi.fn(async () => ({ joined: true, role: "viewer", status: "active" }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    browseNamespaces: vi.fn(async () => browse),
    leaveNamespace: (...a: unknown[]) => leaveNamespace(...(a as [])),
    joinNamespace: (...a: unknown[]) => joinNamespace(...(a as [])),
  };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve(mine) }));
vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn() }));
vi.mock("../flows/NamespaceSettingsDialog", () => ({
  NamespaceSettingsDialog: ({ open, namespace }: { open: boolean; namespace: string }) =>
    open ? <div role="dialog" aria-label={`Settings for ${namespace}`} /> : null,
}));
vi.mock("../flows/MembersDialog", () => ({
  MembersDialog: ({ open, namespace }: { open: boolean; namespace: string }) =>
    open ? <div role="dialog" aria-label={`Members of ${namespace}`} /> : null,
}));

beforeEach(() => {
  leaveNamespace.mockClear();
  useStore.setState({
    authed: true,
    whoami: { authenticated: true, subject: "alice", org: "acme" },
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    view: "browse",
  });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

const row = (name: string) => {
  const el = screen.getAllByRole("button", { name }).find((b) => b.hasAttribute("data-row-open"));
  if (!el) throw new Error(`no row ${name}`);
  return el.closest<HTMLElement>(".list-row") as HTMLElement;
};

describe("Spaces: the rows", () => {
  it("Create namespace is the page header's primary, and the page passes axe", async () => {
    const { container } = render(<Browse />);
    await screen.findAllByText("alpha");
    const header = screen.getByRole("heading", { level: 1, name: "Spaces" }).parentElement as HTMLElement;
    expect(within(header).getByRole("button", { name: "Create namespace" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Create namespace" })).toHaveLength(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("a space you manage: mono name, visibility, description; Invite inline; Settings, Guide, Copy name and Leave in its menu", async () => {
    const user = userEvent.setup();
    render(<Browse />);
    const managed = (await screen.findByRole("heading", { name: /Namespaces you manage/ })).closest(
      "section",
    ) as HTMLElement;
    expect(managed.querySelector("svg[aria-label='star'], [data-icon='star']")).toBeNull();
    const team = within(managed).getByText("team");
    expect(team.tagName).toBe("CODE");
    const r = row("team");
    expect(within(r).getByText("Private")).toBeInTheDocument();
    expect(within(r).getByText("The platform team")).toBeInTheDocument();
    await user.click(within(r).getByRole("button", { name: "Invite people to team" }));
    expect(screen.getByRole("dialog", { name: "Members of team" })).toBeInTheDocument();
    await user.click(within(r).getByRole("button", { name: "Actions for team" }));
    const menu = await screen.findByRole("menu", { name: "Actions for team" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Settings", "Guide", "Copy name", "Leave"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Settings" }));
    expect(screen.getByRole("dialog", { name: "Settings for team" })).toBeInTheDocument();
  });

  it("a public space: its members at the right; Join, or Request upgrade for a member; Leave only for a member", async () => {
    const user = userEvent.setup();
    render(<Browse />);
    await screen.findAllByText("alpha");
    const alpha = row("alpha");
    expect(within(alpha).getByText("Public")).toBeInTheDocument();
    expect(within(alpha).getByText("1 member")).toBeInTheDocument();
    expect(within(alpha).getByText("Support team runbooks")).toBeInTheDocument();
    // A non-member reads the summary only; a member opens the guide from it.
    expect(screen.queryByRole("button", { name: /^Guide: One runbook/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Guide: Beta's conventions" }));
    expect(await screen.findByRole("dialog", { name: "Guide for beta" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    // The guide's summary is the directory's view of it, for members or not.
    expect(within(alpha).getByText("Guide: One runbook per service, keyed runbook/<service>")).toBeInTheDocument();
    expect(within(alpha).getByRole("button", { name: "Join" })).toBeInTheDocument();
    await user.click(within(alpha).getByRole("button", { name: "Actions for alpha" }));
    let menu = await screen.findByRole("menu", { name: "Actions for alpha" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((m) => m.textContent),
    ).toEqual(["Copy name"]);
    await user.keyboard("{Escape}");

    const beta = row("beta");
    expect(within(beta).getByText("2 members")).toBeInTheDocument();
    expect(within(beta).getByRole("button", { name: "Request upgrade" })).toBeInTheDocument();
    await user.click(within(beta).getByRole("button", { name: "Actions for beta" }));
    menu = await screen.findByRole("menu", { name: "Actions for beta" });
    // A member opens the guide; a non-member (alpha) is not offered one it could not read.
    expect(within(menu).getByRole("menuitem", { name: "Guide" })).toBeInTheDocument();
    await user.click(within(menu).getByRole("menuitem", { name: "Leave" }));
    await waitFor(() => expect(leaveNamespace).toHaveBeenCalledWith("acme", "beta"));
  });

  it("your personal namespace offers its guide like any namespace you manage", async () => {
    mine.push({
      namespace: "personal-alice",
      role: "admin",
      status: "active",
      visibility: "private",
      description: "",
      joined_at: null,
      is_new: false,
      kind: "memory",
    });
    try {
      const user = userEvent.setup();
      render(<Browse />);
      await screen.findAllByText("alpha");
      await user.click(within(row("personal-alice")).getByRole("button", { name: "Actions for personal-alice" }));
      const menu = await screen.findByRole("menu", { name: "Actions for personal-alice" });
      expect(within(menu).getByRole("menuitem", { name: "Guide" })).toBeInTheDocument();
    } finally {
      mine.pop();
    }
  });

  it("the rows are one tab stop; the arrows move between them, across both sections", async () => {
    const user = userEvent.setup();
    render(<Browse />);
    await screen.findAllByText("alpha");
    const opens = [...document.querySelectorAll<HTMLElement>("[data-row-open]")];
    expect(opens.map((b) => b.textContent)).toEqual(["team", "alpha", "beta"]);
    expect(opens.map((b) => b.tabIndex)).toEqual([0, -1, -1]);
    opens[0].focus();
    await user.keyboard("{ArrowDown}");
    expect(opens[1]).toHaveFocus();
    await user.keyboard("{End}");
    expect(opens[2]).toHaveFocus();
  });
});

describe("Spaces: requesting a private namespace by name", () => {
  it("empties the search once the typed name is joined", async () => {
    render(<Browse />);
    await screen.findByText("Support team runbooks");
    const box = screen.getByRole("textbox", { name: "Search namespaces" });
    await userEvent.type(box, "secret-ops");
    await userEvent.click(await screen.findByRole("button", { name: "Request access" }));
    await waitFor(() => expect(box).toHaveValue(""));
    expect(joinNamespace).toHaveBeenCalledWith("acme", "secret-ops", { role: "viewer" });
    // The card names the deferred text, so it leaves a render after the box empties.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Request access" })).toBeNull());
  });
});
