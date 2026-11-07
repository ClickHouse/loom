import { act, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";

/** The badge reads leave after paint + idle (two animation frames, then a timer in
 *  jsdom); give them that long before asserting they ran — or that they did not. */
async function painted(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 120));
  });
}
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { Sidebar } from "./Sidebar";
import { invalidateShared } from "../lib/sharedReads";

// The Admin drawer — and the conflicts.list / cost.budget_status / agents.connected polls
// that badge it — exist only for an admin of the current namespace, so a viewer-only
// token sees no admin entry and draws no AUTH_INSUFFICIENT 403s.

// Each icon wrapped in its name, so a test can tell which one an entry shows.
vi.mock("@clickhouse/click-ui/Icon", async (importOriginal) => {
  const real = await importOriginal<typeof import("@clickhouse/click-ui/Icon")>();
  return {
    ...real,
    Icon: (props: Parameters<typeof real.Icon>[0]) => (
      <span data-icon={props.name}>
        <real.Icon {...props} />
      </span>
    ),
  };
});
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    countConflicts: vi.fn(async () => 0),
    getBudgetStatus: vi.fn(async () => ({ over_cap: false })),
    countNewAgents: vi.fn(async () => 0), // The badge asks for a number
    listProposals: vi.fn(async () => ({ proposals: [], total: 0 })),
  };
});
// The sidebar asks through sharedHasAutoResolved, which calls api/audit.
vi.mock("../api/audit", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/audit")>()),
  hasAutoResolved: vi.fn(async () => false),
}));
import { getBudgetStatus, countConflicts, countNewAgents, listProposals } from "../api";
import { hasAutoResolved } from "../api/audit";

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[] | null) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: "dev", namespace: "team-a", agent: "loom-ui" },
    view: "memories",
    sidebarCollapsed: false,
  });
}

const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["team-a"],
};

describe("Sidebar admin drawer", () => {
  beforeEach(() => {
    invalidateShared(); // the budget read is shared with the Usage page
    // The store's sidebar state reads localStorage at module load; the
    // matchMedia-free jsdom renders the desktop rail (`hidden md:flex` is a
    // class, not a media query in jsdom).
    vi.clearAllMocks();
  });
  afterEach(() => {
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("is hidden for a viewer, and none of the admin polls fire", async () => {
    seed(viewer, [row("team-a", "viewer")]);
    render(<Sidebar />);
    expect(screen.getByText("Memories")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Admin/ })).not.toBeInTheDocument();
    // Let any effect that would have polled run.
    await painted();
    expect(countConflicts).not.toHaveBeenCalled();
    expect(getBudgetStatus).not.toHaveBeenCalled();
    expect(countNewAgents).not.toHaveBeenCalled();
    expect(hasAutoResolved).not.toHaveBeenCalled();
  });

  it("is shown for an admin grant on the current namespace, and the polls run", async () => {
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-a", "admin")]);
    render(<Sidebar />);
    expect(screen.getByRole("button", { name: /Admin/ })).toBeInTheDocument();
    await painted();
    expect(countConflicts).toHaveBeenCalledTimes(1);
    expect(getBudgetStatus).toHaveBeenCalledTimes(1);
  });

  it("is hidden when the admin grant is on a different namespace", () => {
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-b", "admin"), row("team-a", "writer")]);
    render(<Sidebar />);
    expect(screen.queryByRole("button", { name: /Admin/ })).not.toBeInTheDocument();
  });

  // A token-only principal (jwt-only namespace access, no user row) has an empty or
  // failed list_mine. The drawer gate is fail-open on "unknown", so the polls need their
  // own positive admin signal or that principal fires conflicts.list + cost.budget_status
  // every 30 s and draws AUTH_INSUFFICIENT each time.
  it("a viewer whose list_mine is empty polls nothing", async () => {
    seed(viewer, []);
    render(<Sidebar />);
    await painted();
    expect(countConflicts).not.toHaveBeenCalled();
    expect(getBudgetStatus).not.toHaveBeenCalled();
    expect(countNewAgents).not.toHaveBeenCalled();
  });

  it("a viewer whose list_mine failed (unknown role) polls nothing", async () => {
    seed(viewer, null);
    render(<Sidebar />);
    await painted();
    expect(countConflicts).not.toHaveBeenCalled();
    expect(getBudgetStatus).not.toHaveBeenCalled();
    expect(countNewAgents).not.toHaveBeenCalled();
  });

  it("an admin claim polls even when list_mine failed", async () => {
    seed({ ...viewer, roles: ["ADMIN"] }, null);
    render(<Sidebar />);
    await painted();
    expect(countConflicts).toHaveBeenCalledTimes(1);
    expect(getBudgetStatus).toHaveBeenCalledTimes(1);
    expect(countNewAgents).toHaveBeenCalledTimes(1);
  });

  // A badge belongs to the namespace it was read for: a switch must not carry the previous
  // namespace's numbers onto the new one's entries, nor into its session cache.
  it("a namespace switch reads the new namespace's agents count on its first read", async () => {
    sessionStorage.clear();
    vi.mocked(countNewAgents).mockResolvedValueOnce(3).mockResolvedValueOnce(1);
    const admin: WhoAmI = { ...viewer, roles: ["WRITER"] };
    seed(admin, [row("team-a", "admin"), row("team-b", "admin")]);
    useStore.setState({ view: "admin:agents" });
    render(<Sidebar />);
    await painted();
    expect(screen.getByRole("link", { name: /Agents/ })).toHaveTextContent("3");
    // Two more reads in team-a; the agents count is asked only every fifth one.
    for (let i = 0; i < 2; i++) {
      act(() => useStore.setState({ whoami: { ...admin } }));
      await painted();
    }
    expect(countNewAgents).toHaveBeenCalledTimes(1);
    act(() => useStore.setState({ identity: { org: "dev", namespace: "team-b", agent: "loom-ui" } }));
    await painted();
    expect(countNewAgents).toHaveBeenCalledTimes(2);
    expect(countNewAgents).toHaveBeenLastCalledWith(expect.objectContaining({ namespace: "team-b" }));
    expect(screen.getByRole("link", { name: /Agents/ })).toHaveTextContent("1");
    const cached: unknown = JSON.parse(sessionStorage.getItem("loom_badge_v1:badges:dev/team-b") ?? "null");
    expect(cached).toMatchObject({ v: { newAgents: 1 } });
  });

  it("a switch to a namespace that is not polled clears the previous namespace's badges", async () => {
    sessionStorage.clear();
    vi.mocked(countConflicts).mockResolvedValueOnce(4);
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-a", "admin")]);
    useStore.setState({ view: "admin:health" });
    render(<Sidebar />);
    await painted();
    expect(screen.getByRole("link", { name: /Review queue/ })).toHaveTextContent("4");
    // team-b's role is unknown (list_mine failed): the drawer stays (fail-open) but nothing
    // is polled, so nothing from team-a may stay on it.
    act(() =>
      useStore.setState({ identity: { org: "dev", namespace: "team-b", agent: "loom-ui" }, myNamespaces: null }),
    );
    await painted();
    expect(screen.getByRole("button", { name: /Admin/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Review queue/ })).not.toBeInTheDocument();
    expect(countConflicts).toHaveBeenCalledTimes(1);
  });

  // With no open conflict, the queue's Auto-resolved tab still has something to show:
  // the entry appears, with no count, so it is reachable without its URL.
  it("shows the Review queue when Loom recently retired a memory on its own", async () => {
    sessionStorage.clear();
    vi.mocked(hasAutoResolved).mockResolvedValueOnce(true);
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-a", "admin")]);
    useStore.setState({ view: "admin:health" });
    render(<Sidebar />);
    await painted();
    expect(hasAutoResolved).toHaveBeenCalledWith({ org: "dev", namespace: "team-a" });
    expect(screen.getByRole("link", { name: /Review queue/ })).not.toHaveTextContent(/\d/);
  });

  // An agent's proposal waiting for a decision lists the Review queue in the drawer.
  it("shows the Review queue when a proposal waits", async () => {
    sessionStorage.clear();
    vi.mocked(listProposals).mockResolvedValueOnce({
      proposals: [{ memory_key: "k", memory_id: "m1", value_preview: "a proposed fact" }],
      total: 2,
    });
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-a", "admin")]);
    useStore.setState({ view: "admin:health" });
    render(<Sidebar />);
    await painted();
    expect(listProposals).toHaveBeenCalledWith({ org: "dev", namespace: "team-a" }, 1);
    expect(screen.getByRole("link", { name: /Review queue/ })).not.toHaveTextContent(/\d/);
    // Proposals alone take the Proposals tab's icon.
    expect(screen.getByRole("link", { name: /Review queue/ }).querySelector("[data-icon]")).toHaveAttribute(
      "data-icon",
      "user",
    );
  });

  it("hides the Review queue with no open conflict, recent auto-resolution or waiting proposal", async () => {
    sessionStorage.clear();
    seed({ ...viewer, roles: ["WRITER"] }, [row("team-a", "admin")]);
    useStore.setState({ view: "admin:health" });
    render(<Sidebar />);
    await painted();
    expect(hasAutoResolved).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("link", { name: /Review queue/ })).not.toBeInTheDocument();
  });

  it("is shown for a platform admin regardless of grants, with the Users entry", async () => {
    seed({ ...viewer, roles: ["PLATFORM_ADMIN"] }, []);
    render(<Sidebar />);
    expect(screen.getByRole("button", { name: /Admin/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Admin/ })).toHaveAttribute("aria-expanded", "false");
  });
});

describe("Sidebar collapsed rail", () => {
  afterEach(() => {
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, sidebarCollapsed: false });
  });

  // The rail shows icons only, so each entry names itself in the shared tooltip, reachable
  // from the keyboard as well as on hover.
  it("names a rail entry in a tooltip on keyboard focus", async () => {
    seed(viewer, [row("team-a", "viewer")]);
    useStore.setState({ sidebarCollapsed: true });
    render(<Sidebar />);
    const link = screen.getByRole("link", { name: "Memories" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    act(() => link.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Memories");
  });

  it("draws no tooltip on the expanded sidebar, where the label is visible", async () => {
    const user = userEvent.setup();
    seed(viewer, [row("team-a", "viewer")]);
    render(<Sidebar />);
    await user.hover(screen.getByRole("link", { name: "Memories" }));
    // Past the tooltip's open delay.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 400));
    });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    // The scope hint is the link's description instead.
    expect(screen.getByRole("link", { name: "Memories" })).toHaveAccessibleDescription(/picked above/);
  });

  // Click UI's slide-out is |← (toward the rail's edge) and slide-in is →|.
  it("points the toggle the way it moves the rail", () => {
    seed(viewer, [row("team-a", "viewer")]);
    const { unmount } = render(<Sidebar />);
    const glyph = (name: string) =>
      screen.getByRole("button", { name }).querySelector("svg")?.getAttribute("aria-label");
    expect(glyph("Collapse sidebar")).toBe("slide-out");
    unmount();
    useStore.setState({ sidebarCollapsed: true });
    render(<Sidebar />);
    expect(glyph("Expand sidebar")).toBe("slide-in");
  });
});
