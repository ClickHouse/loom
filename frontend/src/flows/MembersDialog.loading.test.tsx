/** The re-read after a member change keeps the list on screen, marked busy, instead of
 *  swapping it for a loader; the first read shows the labelled loader. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "../test/rtl";
import { MembersDialog } from "./MembersDialog";
import { useStore } from "../lib/state";
import * as api from "../api";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listGrants: vi.fn(), approveGrant: vi.fn(async () => ({})) };
});

const PENDING = {
  subject: "reporter-private",
  role: "viewer",
  status: "pending",
  pending_since: "2026-09-17T14:50:28Z",
  granted_at: "2026-09-17T14:50:28Z",
  granted_by: "reporter-private",
};

beforeEach(() => {
  vi.mocked(api.listGrants).mockReset();
  useStore.setState({ whoami: { authenticated: true, subject: "alice", org: "clickhouse", roles: ["ADMIN"] } });
});

describe("MembersDialog loading", () => {
  it("names what it is loading on the first read", async () => {
    vi.mocked(api.listGrants).mockReturnValue(new Promise(() => {}));
    render(<MembersDialog open onOpenChange={() => {}} org="clickhouse" namespace="team" />);
    expect(await screen.findByText("Loading members")).toBeInTheDocument();
  });

  it("keeps the members on screen, marked busy, while the re-read after an approval is out", async () => {
    vi.mocked(api.listGrants).mockResolvedValueOnce({ rows: [PENDING] } as never);
    render(<MembersDialog open onOpenChange={() => {}} org="clickhouse" namespace="team" />);
    const approve = await screen.findByRole("button", { name: /Approve/ });
    let answer: (v: unknown) => void = () => {};
    vi.mocked(api.listGrants).mockReturnValueOnce(new Promise((r) => (answer = r)) as never);
    fireEvent.click(approve);
    await waitFor(() => expect(api.listGrants).toHaveBeenCalledTimes(2));
    expect(screen.getByText("reporter-private")).toBeInTheDocument();
    expect(screen.getByTestId("members-body")).toHaveAttribute("aria-busy", "true");
    await act(async () => answer({ rows: [{ ...PENDING, status: "active" }] }));
    expect(screen.getByTestId("members-body")).not.toHaveAttribute("aria-busy");
  });
});
