/** The members dialog reads every page of the grants, draws them a step at a time, says so
 *  when it stops at its page limit (for members, readers and a parent's members), and stops
 *  reading when it closes. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { useStore } from "../lib/state";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listGrants: vi.fn() };
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
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["ADMIN"] } });
});

/** Pages that never end: the reader stops at its limit. */
function endless() {
  let n = 0;
  vi.mocked(api.listGrants).mockImplementation(async () => ({ rows: [grant(`p${n++}`)], next_cursor: "more" }));
}

describe("MembersDialog paging", () => {
  it("says only the first 10,000 members are listed when the pages go on", async () => {
    endless();
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="team" />);
    expect(await screen.findByText("Only the first 10,000 members are listed here.")).toBeInTheDocument();
    expect(api.listGrants).toHaveBeenCalledTimes(20);
  });

  it("says readers in a repository's dialog", async () => {
    endless();
    render(<MembersDialog open onOpenChange={() => {}} org="acme" namespace="code-x" kind="code" />);
    expect(await screen.findByText("Only the first 10,000 readers are listed here.")).toBeInTheDocument();
  });

  it("says a parent's list was cut as its members there", async () => {
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
    expect(through).toHaveTextContent("Only the first 10,000 members there are listed.");
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
    expect(within(through).getByText("80 there")).toBeInTheDocument();
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
});
