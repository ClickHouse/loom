import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BrowseEntry, MyNamespaceEntry } from "../api";
import { useStore } from "../lib/state";
import { axeViolations } from "../test/axe";
import { Browse } from "./Browse";

// A namespace is one list row: the namespace in mono and its visibility badge
// on line 1, its description on line 2, its guide's summary on line 3 when it has one (a
// link to the guide for a member); at the right its member count, the one inline action
// (Invite where you manage it, else Join, Pending or Request upgrade) and a More menu
// (Settings for a manager, Guide for a member where no summary is shown, Copy name, Leave
// for a member). The rows are one tab stop (`useRowNav`); Create namespace is the page
// header's primary.

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
    // A namespace with no guide yet, whichever one the dialog reads.
    getNamespaceGuide: vi.fn(async () => ({
      found: false,
      revision: 0,
      content: "",
      summary: "",
      note: "",
      updated_by: "",
      updated_at: null,
    })),
  };
});
const mineRead = vi.hoisted(() => ({ fails: false }));
vi.mock("../lib/sharedReads", () => ({
  sharedMine: () => (mineRead.fails ? Promise.reject(new Error("down")) : Promise.resolve(mine)),
}));
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

describe("Namespaces in your org: the rows", () => {
  it("Create namespace is the page header's primary, and the page passes axe", async () => {
    const { container } = render(<Browse />);
    await screen.findAllByText("alpha");
    const header = screen.getByRole("heading", { level: 1, name: "Namespaces in your org" })
      .parentElement as HTMLElement;
    expect(within(header).getByRole("button", { name: "Create namespace" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Create namespace" })).toHaveLength(1);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("a namespace you manage: mono name, visibility, description; Invite inline; Settings, Guide, Copy name and Leave in its menu", async () => {
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
    // beta's guide opens from its summary link, so its menu does not offer it twice.
    expect(within(menu).queryByRole("menuitem", { name: "Guide" })).toBeNull();
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

  it("a namespace you manage offers its writer the guide's editor", async () => {
    const user = userEvent.setup();
    render(<Browse />);
    await screen.findAllByText("alpha");
    await user.click(within(row("team")).getByRole("button", { name: "Actions for team" }));
    const menu = await screen.findByRole("menu", { name: "Actions for team" });
    await user.click(within(menu).getByRole("menuitem", { name: "Guide" }));
    await user.click(await screen.findByRole("button", { name: "Write the guide" }));
    // A first guide from the namespaces page starts on the same outline, which does not save unchanged.
    const editor = await screen.findByLabelText("Guide (Markdown)");
    expect((editor as HTMLTextAreaElement).value).toContain("## What this namespace holds");
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true");
  });

  it("a namespace a viewer token names without a grant offers its guide to read, Write unavailable", async () => {
    // `jwt`: the namespace is the token's, and its claim (viewer) is the role.
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["viewer"] } });
    browse.push({
      namespace: "tokenonly",
      description: "",
      kind: "memory",
      owner_email: "",
      member_count: 1,
      created_at: "",
    });
    mine.push({
      namespace: "tokenonly",
      role: "jwt",
      status: "active",
      visibility: "public",
      description: "",
      joined_at: null,
      is_new: false,
      kind: "memory",
    });
    try {
      const user = userEvent.setup();
      render(<Browse />);
      await screen.findAllByText("tokenonly");
      await user.click(within(row("tokenonly")).getByRole("button", { name: "Actions for tokenonly" }));
      const menu = await screen.findByRole("menu", { name: "Actions for tokenonly" });
      await user.click(within(menu).getByRole("menuitem", { name: "Guide" }));
      expect(await screen.findByText(/No guide yet\. Agents that write here/)).toBeInTheDocument();
      const write = screen.getByRole("button", { name: "Write the guide" });
      expect(write).toHaveAttribute("aria-disabled", "true");
      expect(write).toHaveAccessibleDescription("Needs writer on tokenonly. An admin of it can grant that.");
    } finally {
      browse.pop();
      mine.pop();
    }
  });

  it("lists the namespaces when list_mine fails, offering no guide it could not gate", async () => {
    mineRead.fails = true;
    try {
      render(<Browse />);
      expect((await screen.findAllByText("alpha")).length).toBeGreaterThan(0);
      // Membership unread: no row is the caller's, so none offers its guide.
      expect(screen.queryByRole("menuitem", { name: "Guide" })).toBeNull();
    } finally {
      mineRead.fails = false;
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

  it("a collection you manage is not a namespace you manage: the Library holds it", async () => {
    const before = [...mine];
    mine.push({
      namespace: "col-research",
      role: "admin",
      status: "active",
      visibility: "private",
      description: "",
      joined_at: null,
      is_new: false,
      kind: "collection",
      title: "Research",
    });
    mine.push({
      namespace: "col-me-alice",
      role: "admin",
      status: "active",
      visibility: "private",
      description: "",
      joined_at: null,
      is_new: false,
      kind: "personal",
    });
    try {
      const user = userEvent.setup();
      render(<Browse />);
      const managed = (await screen.findByRole("heading", { name: /Namespaces you manage/ })).closest(
        "section",
      ) as HTMLElement;
      expect(within(managed).queryByText(/col-research|Research/)).toBeNull();
      expect(screen.queryByText("col-research")).toBeNull();
      expect(screen.queryByText("col-me-alice")).toBeNull();
      expect(screen.getByText(/Collections you manage are in the/)).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Library" }));
      expect(useStore.getState().view).toBe("knowledge");
    } finally {
      mine.splice(0, mine.length, ...before);
    }
  });
});

describe("Namespaces in your org: requesting a private namespace by name", () => {
  it("offers the request for a name no public row carries, beside the rows that match it", async () => {
    browse.push({ ...browse[1], namespace: "support-team", guide_summary: "", member_count: 3 });
    try {
      render(<Browse />);
      await screen.findByText("support-team");
      const box = screen.getByRole("textbox", { name: "Search namespaces" });
      await userEvent.type(box, "support");
      expect(await screen.findByText("No public namespace named", { exact: false })).toHaveTextContent(
        "No public namespace named support.",
      );
      expect(within(screen.getByTestId("browse-list")).getByText("support-team")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Request access" })).toBeInTheDocument();
      await userEvent.clear(box);
      await userEvent.type(box, "support-team");
      await waitFor(() => expect(screen.queryByRole("button", { name: "Request access" })).toBeNull());
      expect(within(screen.getByTestId("browse-list")).getByText("support-team")).toBeInTheDocument();
    } finally {
      browse.pop();
    }
  });

  it("offers no request for a name a listed row carries in another case, or for a phrase", async () => {
    browse.push({ ...browse[1], namespace: "support-team", guide_summary: "", member_count: 3 });
    try {
      render(<Browse />);
      await screen.findByText("support-team");
      const box = screen.getByRole("textbox", { name: "Search namespaces" });
      await userEvent.type(box, "Support-Team");
      expect(await within(screen.getByTestId("browse-list")).findByText("support-team")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
      await userEvent.clear(box);
      // Matches alpha's description, but is no namespace name.
      await userEvent.type(box, "support team");
      expect(await within(screen.getByTestId("browse-list")).findByText("alpha")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
    } finally {
      browse.pop();
    }
  });

  it("says nothing matches a search that is no namespace name, and offers no request", async () => {
    render(<Browse />);
    await screen.findByText("Support team runbooks");
    await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "my_ns");
    expect(await screen.findByText(/No public namespaces match/)).toHaveTextContent("No public namespaces match my_ns");
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("textbox", { name: "Search namespaces" })).toHaveValue("");
  });

  it("offers no request for a namespace the caller admins", async () => {
    render(<Browse />);
    await screen.findByText("Support team runbooks");
    await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "team");
    expect(await within(screen.getByTestId("browse-list")).findByText("alpha")).toBeInTheDocument();
    expect(screen.queryByText("No public namespace named", { exact: false })).toBeNull();
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
  });

  it("shows an admin's private namespace under the ones they manage, and no request for it", async () => {
    const description = browse[0].description;
    browse[0].description = "Runbooks";
    try {
      render(<Browse />);
      await screen.findByText("Runbooks");
      await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "team");
      expect(await screen.findByText(/No public namespaces match/)).toHaveTextContent(
        "No public namespaces match team",
      );
      expect(screen.getByText("The platform team")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
      await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
      expect(screen.getByRole("textbox", { name: "Search namespaces" })).toHaveValue("");
    } finally {
      browse[0].description = description;
    }
  });

  it("points a collection's name to the Library instead of a request", async () => {
    render(<Browse />);
    await screen.findByText("Support team runbooks");
    await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "col-research");
    const line = await screen.findByText(/is a collection: it is shared in the/);
    expect(line).toHaveTextContent("col-research is a collection: it is shared in the Library.");
    expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
    expect(screen.queryByText(/No public namespaces match/)).toBeNull();
    await userEvent.click(within(line).getByRole("button", { name: "Library" }));
    expect(useStore.getState().view).toBe("knowledge");
  });

  it("offers neither a request nor a Library line for a personal collection or a bare prefix", async () => {
    render(<Browse />);
    await screen.findByText("Support team runbooks");
    const box = screen.getByRole("textbox", { name: "Search namespaces" });
    for (const name of ["col-me-0123456789abcdef", "col-"]) {
      await userEvent.clear(box);
      await userEvent.type(box, name);
      expect(await screen.findByText(/No public namespaces match/)).toHaveTextContent(
        `No public namespaces match ${name}`,
      );
      expect(screen.queryByText(/is a collection/)).toBeNull();
      expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
    }
  });

  it("points a repository's exact name to Repositories instead of a request", async () => {
    browse.push({ ...browse[1], namespace: "code-loom", kind: "code", guide_summary: "" });
    try {
      render(<Browse />);
      await screen.findByText("Support team runbooks");
      await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "code-loom");
      const line = await screen.findByText(/is a repository: request it from/);
      expect(line).toHaveTextContent("code-loom is a repository: request it from Repositories.");
      expect(screen.queryByRole("button", { name: "Request access" })).toBeNull();
      await userEvent.click(within(line).getByRole("button", { name: "Repositories" }));
      expect(useStore.getState().view).toBe("code");
    } finally {
      browse.pop();
    }
  });

  it("offers a viewer of a private namespace the higher role beside a partly matching public row", async () => {
    const before = [...mine];
    mine.push({ ...mine[0], namespace: "sup", role: "viewer" });
    try {
      render(<Browse />);
      await screen.findByText("Support team runbooks");
      await userEvent.type(screen.getByRole("textbox", { name: "Search namespaces" }), "sup");
      expect(await screen.findByText("No public namespace named", { exact: false })).toHaveTextContent(
        "No public namespace named sup.",
      );
      expect(within(screen.getByTestId("browse-list")).getByText("alpha")).toBeInTheDocument();
      expect(screen.getByText(/Pick a higher role above to request an upgrade/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Request access" })).toBeDisabled();
    } finally {
      mine.splice(0, mine.length, ...before);
    }
  });

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
