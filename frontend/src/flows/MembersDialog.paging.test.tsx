/** The members dialog reads every page of the grants, says so when it stops at its page
 *  limit (for members, readers and a parent's members), and stops reading when it closes. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "../test/rtl";
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
});
