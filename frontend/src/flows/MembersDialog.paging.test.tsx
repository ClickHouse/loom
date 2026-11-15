/** The members dialog reads every page of the grants, draws them a step at a time, says so
 *  when it stops at its page limit (for members, readers and a parent's members), and stops
 *  reading when it closes. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { useStore } from "../lib/state";
import * as api from "../api";

const confirm = vi.hoisted(() => vi.fn(async (_opts: { title: string; message: string }) => false));
vi.mock("../components/ConfirmDialog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/ConfirmDialog")>()),
  confirmAction: confirm,
}));

const toast = vi.hoisted(() => vi.fn());
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast,
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listGrants: vi.fn(),
    createGrant: vi.fn(async () => ({})),
    isAuthDisabled: vi.fn(() => false),
  };
});

const at = "2026-09-17T14:50:28Z";
const grant = (subject: string) => ({
  subject,
  role: "viewer" as const,
  status: "active" as const,
  granted_at: at,
  granted_by: "alice",
});

beforeEach(() => {
  vi.mocked(api.listGrants).mockReset();
  confirm.mockClear();
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["ADMIN"] } });
});

/** Pages that never end: the reader stops at its limit. */
function endless() {
  let n = 0;
  vi.mocked(api.listGrants).mockImplementation(async () => ({ rows: [grant(`p${n++}`)], next_cursor: "more" }));
}

describe("MembersDialog paging", () => {
  it("says only the first 10,000 grants were read when the pages go on, pending ones among them", async () => {
    // Half of every page pending: the note counts grants read, not members.
    let n = 0;
    vi.mocked(api.listGrants).mockImplementation(async () => ({
      rows: [grant(`p${n++}`), { ...grant(`q${n++}`), status: "pending" as const }],
      next_cursor: "more",
    }));
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByText("Only the first 10,000 grants were read.")).toBeInTheDocument();
    expect(screen.getByText("Pending requests (20)")).toBeInTheDocument();
    expect(api.listGrants).toHaveBeenCalledTimes(20);
  });

  it("words a repository's cut read the same way", async () => {
    endless();
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="code-x" kind="code" />);
    expect(await screen.findByText("Only the first 10,000 grants were read.")).toBeInTheDocument();
  });

  it("says a parent's list was cut as its grants there", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("alice")] });
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [grant("bob")], cut: true }]}
      />,
    );
    const through = await screen.findByRole("region", { name: "Also can see it, through Engineering" });
    expect(through).toHaveTextContent("Only the first 10,000 grants there were read.");
  });

  it("stops reading pages once it closes", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(api.listGrants).mockImplementation(async (_id, opts) => {
      if (opts?.signal) signals.push(opts.signal);
      return new Promise(() => {});
    });
    const { unmount } = render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    await screen.findByText("Loading members");
    expect(signals).toHaveLength(1);
    unmount();
    expect(signals[0].aborted).toBe(true);
  });

  it("draws the first 50 of 1,200 members for an admin, Show more adds 50, and the count says 1,200", async () => {
    const rows = Array.from({ length: 1200 }, (_, i) => grant(`m${i}@example.com`));
    vi.mocked(api.listGrants).mockImplementation(async (_id, opts) =>
      opts?.cursor ? { rows: rows.slice(500), next_cursor: null } : { rows: rows.slice(0, 500), next_cursor: "p2" },
    );
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByText("Members (1,200)")).toBeInTheDocument();
    const list = () => screen.getByText("m0@example.com").closest("ul") as HTMLElement;
    expect(within(list()).getAllByRole("listitem")).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "Show 50 more" }));
    expect(within(list()).getAllByRole("listitem")).toHaveLength(100);
    expect(screen.getByText("m99@example.com")).toBeInTheDocument();
  });

  it("draws a parent's members a step at a time too, and marks you under another spelling", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("alice")] });
    const theirs = [
      { ...grant("alice@example.com"), also: ["alice"] },
      ...Array.from({ length: 79 }, (_, i) => grant(`p${i}`)),
    ];
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: theirs }]}
      />,
    );
    const through = await screen.findByRole("region", { name: "Also can see it, through Engineering" });
    expect(within(through).getByText("Members there (80)")).toBeInTheDocument();
    expect(within(through).getAllByRole("listitem")).toHaveLength(50);
    expect(within(through).getByText("alice@example.com").parentElement).toHaveTextContent("(you)");
    fireEvent.click(within(through).getByRole("button", { name: "Show 30 more" }));
    expect(within(through).getAllByRole("listitem")).toHaveLength(80);
  });

  it("says nothing of an empty list while who else can see it is read", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [] });
    const { rerender } = render(
      <MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" inherited={null} />,
    );
    expect(await screen.findByText("Loading who else can see it")).toBeInTheDocument();
    expect(screen.queryByText(/No members yet|No one is named here/)).not.toBeInTheDocument();
    rerender(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [grant("bob")] }]}
      />,
    );
    expect(await screen.findByText(/No one is named here; who can see it comes from/)).toBeInTheDocument();
    expect(screen.queryByText(/^No members yet/)).not.toBeInTheDocument();
  });

  it("keeps the count after the last Show more, and hands focus to the first row it added", async () => {
    const rows = Array.from({ length: 80 }, (_, i) => grant(`m${i}@example.com`));
    vi.mocked(api.listGrants).mockResolvedValue({ rows });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByText("Members (80)")).toBeInTheDocument();
    const more = screen.getByRole("button", { name: "Show 30 more" });
    more.focus();
    fireEvent.click(more);
    expect(screen.queryByRole("button", { name: /^Show \d+ more$/ })).not.toBeInTheDocument();
    expect(screen.getByText("Members (80)")).toBeInTheDocument();
    const added = screen.getByText("m50@example.com").closest("li") as HTMLElement;
    await waitFor(() => expect(added.contains(document.activeElement)).toBe(true));
  });

  it("keeps a parent's count after its last Show more", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("alice")] });
    const theirs = Array.from({ length: 60 }, (_, i) => grant(`p${i}`));
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: theirs }]}
      />,
    );
    const through = await screen.findByRole("region", { name: "Also can see it, through Engineering" });
    fireEvent.click(within(through).getByRole("button", { name: "Show 10 more" }));
    expect(within(through).getByText("Members there (60)")).toBeInTheDocument();
  });

  it("treats a row under another of your spellings as yours: (you), and no role or Remove", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({
      rows: [{ ...grant("alice@example.com"), role: "admin", also: ["alice"] }, grant("bob")],
    });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    const mine = (await screen.findByText("alice@example.com")).closest("li") as HTMLElement;
    expect(mine).toHaveTextContent("(you)");
    expect(screen.queryByRole("button", { name: "Remove alice@example.com" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Role for alice@example.com")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove bob" })).toBeInTheDocument();
  });

  it("gives an org admin no controls on a collection whose role is writer", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "olga", org: "acme", roles: ["ORG_ADMIN"] } });
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" manage={false} />);
    await screen.findByText("bob");
    expect(screen.queryByLabelText("Role for bob")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove bob" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("button", { name: "Add member" })).toHaveAttribute("aria-disabled", "true");
  });

  it("takes admin rights from the person's grant under another spelling, with no claim", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: [] } });
    vi.mocked(api.listGrants).mockResolvedValue({
      rows: [{ ...grant("Alice@example.com"), role: "admin", also: ["ALICE"] }, grant("bob")],
    });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByLabelText("Role for bob")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add member" })).not.toHaveAttribute("aria-disabled");
  });

  it("offers Withdraw on the person's own pending request under another spelling", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: [] } });
    vi.mocked(api.listGrants).mockResolvedValue({
      rows: [{ ...grant("alice@example.com"), status: "pending", also: ["alice"] }],
    });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByRole("button", { name: "Withdraw" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it("says No members yet when no parent names anyone either", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [] });
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [] }]}
      />,
    );
    expect(
      await screen.findByText(/^No members yet\. Click Add member below to invite the first one\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No one is named here/)).not.toBeInTheDocument();
  });

  it("names a row as the header does, with the subject beneath", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({
      rows: [grant("group:eng"), grant("everyone"), grant("sa:reporter-a1b2c3")],
    });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" />);
    const row = (await screen.findByText("group eng")).closest("li") as HTMLElement;
    expect(row).toHaveTextContent("group:eng");
    expect(screen.getByText("Everyone in acme").closest("li")).toHaveTextContent("everyone");
    expect(screen.getByText("Reporter bot").closest("li")).toHaveTextContent("sa:reporter-a1b2c3");
  });

  it("draws more than 50 pending requests a step at a time, the count saying them all", async () => {
    const asks = Array.from({ length: 70 }, (_, i) => ({ ...grant(`ask${i}`), status: "pending" as const }));
    vi.mocked(api.listGrants).mockResolvedValue({ rows: asks });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByText("Pending requests (70)")).toBeInTheDocument();
    const list = () => screen.getByText("ask0").closest("ul") as HTMLElement;
    expect(within(list()).getAllByRole("listitem")).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "Show 20 more" }));
    expect(within(list()).getAllByRole("listitem")).toHaveLength(70);
    expect(screen.getByText("Pending requests (70)")).toBeInTheDocument();
  });

  it("marks a parent's grant under one of your other spellings as yours", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "ada", org: "acme", roles: [] } });
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [{ ...grant("ada"), also: ["ada@example.com"] }] });
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [grant("ada@example.com")] }]}
      />,
    );
    const through = await screen.findByRole("region", { name: "Also can see it, through Engineering" });
    expect(within(through).getByText("ada@example.com").parentElement).toHaveTextContent("(you)");
  });

  it("names a collection by its title in the reason See who gives", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "olga", org: "acme", roles: [] } });
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-q3-plans"
        label="Q3 plans"
        manage={false}
      />,
    );
    await screen.findByText("bob");
    expect(screen.getByRole("button", { name: "Add member" })).toHaveAccessibleDescription(
      "Needs admin on Q3 plans. An admin of it can grant that.",
    );
    expect(screen.getByRole("button", { name: "Remove bob" })).toHaveAccessibleDescription(
      "Needs admin on Q3 plans. An admin of it can grant that.",
    );
  });

  it("says a member removed here can still see it through a parent that names them", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("carol")] });
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        label="Runbooks"
        inherited={[
          { namespace: "col-top", title: "Engineering", rows: [{ ...grant("bob@example.com"), also: ["bob"] }] },
        ]}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0]).toMatchObject({
      title: "Remove bob from Runbooks?",
      message: "Their grant here is revoked. They can still see it through Engineering.",
    });
    fireEvent.click(screen.getByRole("button", { name: "Remove carol" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(confirm.mock.calls[1][0].message).toBe("Their grant here is revoked.");
  });

  it("counts a grant to everyone as naming every member removed, here or on a parent", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
    const { unmount } = render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        label="Runbooks"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [grant("everyone")] }]}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0].message).toBe(
      "Their grant here is revoked. They can still see it through Engineering.",
    );
    unmount();
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("everyone")] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(confirm.mock.calls[1][0].message).toBe("Their grant here is revoked. Everyone in acme can still see it.");
  });

  it("words removing the grant to everyone as no longer sharing, and still names a parent's", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("everyone")] });
    const { unmount } = render(
      <MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" />,
    );
    expect(await screen.findByLabelText("Role for Everyone in acme")).toBeInTheDocument();
    // Its tooltip says what its press does, as its name does.
    expect(screen.getByRole("button", { name: "Stop sharing with everyone in acme" })).not.toHaveAccessibleDescription(
      /Remove from/,
    );
    fireEvent.click(screen.getByRole("button", { name: "Stop sharing with everyone in acme" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0]).toMatchObject({
      title: "Stop sharing Runbooks with everyone in acme?",
      message:
        "The grant here is revoked. Only those named here or on a parent, and the members of groups named, keep access.",
    });
    unmount();
    render(
      <MembersDialog
        open
        onOpenChange={() => {}}
        org="acme"
        namespace="col-a"
        label="Runbooks"
        inherited={[{ namespace: "col-top", title: "Engineering", rows: [grant("everyone")] }]}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Stop sharing with everyone in acme" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(confirm.mock.calls[1][0].message).toBe(
      "The grant here is revoked. Everyone in acme can still see it through Engineering.",
    );
  });

  it("names a service account by its label in Remove, its confirm and its role", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("sa:reporter-a1b2c3")] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" />);
    expect(await screen.findByLabelText("Role for Reporter bot")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove Reporter bot" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0].title).toBe("Remove Reporter bot from Runbooks?");
  });

  it("names the place in the Withdraw confirm", async () => {
    useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: [] } });
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [{ ...grant("alice"), status: "pending" as const }] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" />);
    fireEvent.click(await screen.findByRole("button", { name: "Withdraw" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls.at(-1)?.[0].title).toBe("Withdraw your viewer request on Runbooks?");
  });

  it("describes a collection's roles by what they do with its documents", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add member" }));
    expect(screen.getByText("Read, add, move and delete documents, and add collections inside")).toBeInTheDocument();
  });

  it("says a token may still let a member removed from a namespace in, and not on a collection", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
    const { unmount } = render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0].message).toBe(
      "Their grant here is revoked. A token of theirs may still carry roles that let them in.",
    );
    unmount();
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2));
    expect(confirm.mock.calls[1][0].message).toBe("Their grant here is revoked.");
  });

  it("does not narrow access on stopping sharing with everyone while a parent's grants are unknown", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("everyone")] });
    const unknown = [
      { namespace: "col-top", title: "Engineering", rows: null },
      {
        namespace: "",
        title: "a collection you can't open",
        rows: null,
        note: "Who can see that collection isn't shown to you.",
      },
      { namespace: "col-big", title: "Big", rows: [grant("carol")], cut: true },
    ];
    for (const [i, p] of unknown.entries()) {
      const { unmount } = render(
        <MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" inherited={[p]} />,
      );
      fireEvent.click(await screen.findByRole("button", { name: "Stop sharing with everyone in acme" }));
      await waitFor(() => expect(confirm).toHaveBeenCalledTimes(i + 1));
      expect(confirm.mock.calls[i][0].message).toBe(
        "The grant here is revoked. A collection it sits in may still let others in.",
      );
      unmount();
    }
  });

  it("says a collection it sits in may still let others in while its parents are still being read", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("everyone")] });
    render(
      <MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" inherited={null} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Stop sharing with everyone in acme" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0].message).toBe(
      "The grant here is revoked. A collection it sits in may still let others in.",
    );
  });

  it("says a member removed may still get in through a collection it sits in while its parents are unknown", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
    render(
      <MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" inherited={null} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(confirm.mock.calls[0][0].message).toBe(
      "Their grant here is revoked. A collection it sits in may still let them in.",
    );
  });

  it("claims no narrowing on stopping sharing with sign-in off, in any dialog", async () => {
    vi.mocked(api.isAuthDisabled).mockReturnValue(true);
    try {
      vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob"), grant("everyone")] });
      render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" label="Runbooks" />);
      expect(
        await screen.findByText(/^Sign-in is off: everyone who can reach this server can see it/),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Stop sharing with everyone in acme" }));
      await waitFor(() => expect(confirm).toHaveBeenCalled());
      expect(confirm.mock.calls[0][0].message).toBe("The grant here is revoked.");
    } finally {
      vi.mocked(api.isAuthDisabled).mockReturnValue(false);
    }
  });

  it("says only that a member's grant is revoked on a namespace with sign-in off", async () => {
    vi.mocked(api.isAuthDisabled).mockReturnValue(true);
    try {
      vi.mocked(api.listGrants).mockResolvedValue({ rows: [grant("bob")] });
      render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
      fireEvent.click(await screen.findByRole("button", { name: "Remove bob" }));
      await waitFor(() => expect(confirm).toHaveBeenCalled());
      expect(confirm.mock.calls[0][0].message).toBe("Their grant here is revoked.");
    } finally {
      vi.mocked(api.isAuthDisabled).mockReturnValue(false);
    }
  });

  it("names an added group by its label in the toast", async () => {
    vi.mocked(api.listGrants).mockResolvedValue({ rows: [] });
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="col-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add member" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "group:eng" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Added group eng as writer", "ok"));
  });
});
