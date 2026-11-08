import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setFoldOpen } from "../lib/folds";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { SessionBriefsSection } from "./SessionBriefsSection";

// A brief whose conversation was never recorded is still findable: the
// Conversations page holds the namespace's briefs in one bucket above its threads,
// folded to its title so it never pushes the threads down, and read only once opened.

const listBriefs = vi.fn();
const getBrief = vi.fn();
const deleteBrief = vi.fn();
vi.mock("../api/briefs", () => ({
  listBriefs: (...a: unknown[]) => listBriefs(...a),
  getBrief: (...a: unknown[]) => getBrief(...a),
  deleteBrief: (...a: unknown[]) => deleteBrief(...a),
}));
vi.mock("./ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));

describe("SessionBriefsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    setFoldOpen("session-briefs", false);
  });
  afterEach(() => {
    cleanup();
    useStore.setState({ whoami: null, identity: null, myNamespaces: null });
  });

  it("renders nothing when the namespace holds no brief", async () => {
    listBriefs.mockResolvedValue([]);
    const { container } = render(<SessionBriefsSection org="o" namespace="ns" />);
    await waitFor(() => expect(listBriefs).toHaveBeenCalledWith({ org: "o", namespace: "ns" }, { limit: 20 }));
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each brief, looked up by its key, and opens it in place", async () => {
    listBriefs.mockResolvedValue([{ brief_key: "auto/loom/main" }, { brief_key: "handoff/billing" }]);
    getBrief.mockImplementation(async (_id: unknown, key: string) => ({
      found: true,
      brief_key: key,
      revision: 1,
      revisions: 1,
      rendered: `brief ${key}`,
      updated_at: null,
    }));
    render(<SessionBriefsSection org="o" namespace="ns" />);
    const toggle = await screen.findByRole("button", { name: "Session briefs" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(getBrief).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    await waitFor(() => expect(getBrief).toHaveBeenCalledTimes(2));
    expect(getBrief.mock.calls.map((c) => c[1]).sort()).toEqual(["auto/loom/main", "handoff/billing"]);
    fireEvent.click(await screen.findByRole("button", { name: /handoff\/billing/ }));
    expect(await screen.findByText("brief handoff/billing")).toBeInTheDocument();
    expect(screen.getByText("Restore the session brief handoff/billing.")).toBeInTheDocument();
  });

  // One brief, its section open, its body ready to read.
  const briefs = () => {
    listBriefs.mockResolvedValue([{ brief_key: "handoff/billing", goal: "Billing handoff" }]);
    getBrief.mockResolvedValue({
      found: true,
      brief_key: "handoff/billing",
      revision: 1,
      revisions: 1,
      rendered: "b",
      updated_at: null,
    });
    setFoldOpen("session-briefs", true);
  };
  const as = (role: "viewer" | "writer") =>
    useStore.setState({
      whoami: { authenticated: true, subject: "a@x", org: "o", roles: ["VIEWER"], namespaces: ["ns"] },
      identity: { org: "o", namespace: "ns", agent: "loom-ui" },
      myNamespaces: [{ namespace: "ns", role, visibility: "private", description: "", joined_at: "", is_new: false }],
    });

  it("lets a writer forget a brief, which leaves the list", async () => {
    as("writer");
    briefs();
    deleteBrief.mockResolvedValue({ deleted: true });
    render(<SessionBriefsSection org="o" namespace="ns" />);
    fireEvent.click(await screen.findByRole("button", { name: "Forget brief Billing handoff" }));
    await waitFor(() => expect(deleteBrief).toHaveBeenCalledWith({ org: "o", namespace: "ns" }, "handoff/billing"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Forget brief Billing handoff" })).toBeNull());
  });

  it("offers a viewer no Forget", async () => {
    as("viewer");
    briefs();
    render(<SessionBriefsSection org="o" namespace="ns" />);
    expect(await screen.findByRole("button", { name: /handoff\/billing/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Forget brief Billing handoff" })).toBeNull();
  });
});
