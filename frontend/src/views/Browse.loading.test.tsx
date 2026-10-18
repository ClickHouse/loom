import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// A reload after a create keeps the list on screen and marks it busy, instead of
// swapping it for skeletons; the first load is a labelled skeleton.

const browseNamespaces = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, browseNamespaces: (...args: unknown[]) => browseNamespaces(...args) };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve([]) }));
vi.mock("../flows/CreateNamespaceDialog", () => ({
  CreateNamespaceDialog: ({ open, onCreated }: { open: boolean; onCreated: () => void }) =>
    open ? (
      <button type="button" onClick={onCreated}>
        Fake created
      </button>
    ) : null,
}));

const ROW = { namespace: "alpha", description: "", kind: "memory", requires_approval: false, member_count: 1 };

beforeEach(() => {
  browseNamespaces.mockReset();
  useStore.setState({ authed: true, identity: { org: "acme", namespace: "team", agent: "loom-ui" }, view: "browse" });
});
afterEach(() => {
  cleanup();
  useStore.setState({ identity: null });
});

describe("Browse: loading", () => {
  it("shows a labelled skeleton on the first load", async () => {
    browseNamespaces.mockReturnValue(new Promise(() => {}));
    render(<Browse />);
    expect(await screen.findByRole("status", { name: "Loading namespaces" })).toHaveAttribute("aria-busy", "true");
  });

  it("keeps the rows on screen, marked busy, while a reload is in flight", async () => {
    browseNamespaces.mockResolvedValueOnce([ROW]);
    render(<Browse />);
    expect(await screen.findByText("alpha")).toBeInTheDocument();

    let answer: (rows: unknown[]) => void = () => {};
    browseNamespaces.mockReturnValueOnce(new Promise((r) => (answer = r)));
    fireEvent.click(screen.getByRole("button", { name: "Create namespace" }));
    fireEvent.click(screen.getByRole("button", { name: "Fake created" }));

    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Loading namespaces" })).toBeNull();
    expect(screen.getByTestId("browse-list")).toHaveAttribute("aria-busy", "true");

    await act(async () => answer([ROW, { ...ROW, namespace: "beta" }]));
    expect(await screen.findByText("beta")).toBeInTheDocument();
    expect(screen.getByTestId("browse-list")).not.toHaveAttribute("aria-busy");
  });
});

// Review finding: a failed read showed "No public namespaces yet"; it is the list's error
// state, with its request id and a Retry that reads again.
describe("Browse: a failed read", () => {
  it("says the read failed, never that there is nothing, and Retry reads again", async () => {
    const { ApiError } = await import("../api");
    browseNamespaces.mockRejectedValueOnce(
      new ApiError({ status: 503, code: "BACKEND_UNAVAILABLE", message: "upstream down", requestId: "req-9" }),
    );
    browseNamespaces.mockResolvedValueOnce([ROW]);
    render(<Browse />);
    expect(await screen.findByText("Couldn't load namespaces")).toBeInTheDocument();
    expect(screen.getByText(/req-9/)).toBeInTheDocument();
    expect(screen.queryByText("No public namespaces yet")).toBeNull();
    expect(screen.queryByText(/0 namespaces/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("alpha")).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load namespaces")).toBeNull();
    expect(browseNamespaces).toHaveBeenCalledTimes(2);
  });
});
