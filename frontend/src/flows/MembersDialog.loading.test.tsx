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

describe("MembersDialog failed reads", () => {
  it("says a failed first read failed, with Retry, never 'No members yet'", async () => {
    vi.mocked(api.listGrants)
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValueOnce({ rows: [PENDING] } as never);
    render(<MembersDialog open onOpenChange={() => {}} org="clickhouse" namespace="team" />);
    expect(await screen.findByText("Couldn't load members")).toBeInTheDocument();
    expect(screen.queryByText(/No members yet/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("reporter-private")).toBeInTheDocument();
  });
});

// A request's wait and a member's grant are the list's one time format, quiet in the row.
describe("MembersDialog times", () => {
  it("draws a request's wait and a member's grant time with ListTime", async () => {
    vi.mocked(api.listGrants).mockResolvedValueOnce({
      rows: [
        { ...PENDING, pending_since: "2025-03-05 09:00:00" },
        { ...PENDING, subject: "bob", status: "active", granted_at: "2025-03-04 10:00:00", granted_by: "alice" },
      ],
    } as never);
    render(<MembersDialog open onOpenChange={() => {}} org="clickhouse" namespace="team" />);
    const granted = await screen.findByText(/^Granted/);
    expect(granted.querySelector("time")).toHaveAttribute("dateTime", "2025-03-04T10:00:00.000Z");
    expect(granted).toHaveTextContent("by alice");
    const asked = screen.getByText(/^requested/);
    expect(asked.querySelector("time")).toHaveAttribute("dateTime", "2025-03-05T09:00:00.000Z");
    expect(asked.querySelector("time")).not.toHaveAttribute("tabindex");
  });
});
