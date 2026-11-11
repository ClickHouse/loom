import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getMySharingSummary,
  listCodeRepos,
  listCollections,
  listMine,
  type CodeRepoSummary,
  type MyNamespaceEntry,
  type SharingSummary,
} from "../api";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { Sharing } from "./Sharing";

// A collection the caller holds a grant on is listed apart, by its title, and opens in
// the Library: never among the namespaces, never counted as one.

const MINE: MyNamespaceEntry[] = [
  {
    namespace: "team",
    role: "admin",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  },
  {
    namespace: "col-r",
    role: "writer",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "collection",
    title: "Research",
  },
];

const row = (namespace: string, role: string) => ({
  namespace,
  role,
  visibility: "private",
  member_count: 2,
  writes_7d: 1,
  recalls_7d: 1,
});

const SUMMARY: SharingSummary = {
  subject: "alice",
  org: "acme",
  window_days: 7,
  totals: { writes: 2, recalls: 2, cross_namespace_recalls: 0, namespaces: 2, agents: 0 },
  agents: [],
  agent_namespaces: [],
  namespaces: [row("team", "admin"), row("col-r", "writer")],
} as unknown as SharingSummary;

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => MINE),
    listCodeRepos: vi.fn(async () => []),
    getMySharingSummary: vi.fn(async () => SUMMARY),
    listCollections: vi.fn(async () => ({
      collections: [{ namespace: "col-r", parent: "", personal: false, role: "writer", title: "Research" }],
      next_cursor: null,
    })),
  };
});

beforeEach(() => {
  invalidateShared();
  useStore.setState({
    authed: true,
    whoami: { authenticated: true, subject: "alice", org: "acme", roles: ["WRITER"] },
    myNamespaces: MINE,
    identity: { org: "acme", namespace: "team", agent: "loom-ui" },
    view: "sharing",
  });
});
afterEach(() => cleanup());

describe("Flow: collections", () => {
  it("lists a collection under Collections by title, apart from the namespaces and their count", async () => {
    render(<Sharing />);
    const heading = await screen.findByRole("heading", { name: "Collections" });
    const section = heading.closest("section") as HTMLElement;
    const open = await within(section).findByRole("button", { name: "Research" });
    // Where it sits is the link's description.
    expect(open).toHaveAccessibleDescription("Library");
    const sharing = screen.getByRole("heading", { name: "What you're sharing" }).closest("section") as HTMLElement;
    expect(within(sharing).getByText("team")).toBeInTheDocument();
    expect(within(sharing).queryByText("col-r")).toBeNull();
    expect(screen.queryByText("col-r")).toBeNull();
    // One namespace is active; the collection is counted as a collection.
    await waitFor(() =>
      expect(screen.getByText("Active namespaces").parentElement).toHaveTextContent(/^Active namespaces\s*1$/),
    );
    expect(screen.getByText(/Totals span the 1 namespace and 1 collection you hold a grant on/)).toBeInTheDocument();
    fireEvent.click(open);
    expect(useStore.getState().view).toBe("knowledge");
    expect(new URLSearchParams(window.location.search).get("collection")).toBe("col-r");
  });

  it("counts only the collections when every grant is on one", async () => {
    vi.mocked(getMySharingSummary).mockResolvedValueOnce({
      ...SUMMARY,
      totals: { ...SUMMARY.totals, namespaces: 1 },
      namespaces: [row("col-r", "writer")],
    } as unknown as SharingSummary);
    render(<Sharing />);
    const totals = await screen.findByText(/^Totals span/);
    expect(totals.textContent).toMatch(/^Totals span the 1 collection you hold a grant on\./);
    // No namespace to count: no "Active namespaces 0" beside the collection's writes.
    expect(screen.queryByText("Active namespaces")).toBeNull();
    // No agent was active: Tool activity names no count, never "the 0 namespaces".
    expect(screen.queryByText(/across the/)).toBeNull();
    expect(screen.queryByText(/0 namespaces/)).toBeNull();
  });

  it("counts a repository's branches as one repository, not as namespaces", async () => {
    const branch = (namespace: string): MyNamespaceEntry => ({ ...MINE[0], namespace, role: "viewer", kind: "code" });
    vi.mocked(listMine).mockResolvedValueOnce([...MINE, branch("code-loom-main"), branch("code-loom-dev")]);
    vi.mocked(getMySharingSummary).mockResolvedValueOnce({
      ...SUMMARY,
      totals: { ...SUMMARY.totals, namespaces: 4 },
      namespaces: [...SUMMARY.namespaces, row("code-loom-main", "viewer"), row("code-loom-dev", "viewer")],
    } as unknown as SharingSummary);
    const repo = (namespace: string, branch: string) => ({ namespace, repo: "acme/loom", branch }) as CodeRepoSummary;
    vi.mocked(listCodeRepos).mockResolvedValueOnce([repo("code-loom-main", "main"), repo("code-loom-dev", "dev")]);
    render(<Sharing />);
    await waitFor(() =>
      expect(screen.getByText(/^Totals span/).textContent).toMatch(
        /^Totals span the 1 namespace, 1 repository and 1 collection you hold a grant on\./,
      ),
    );
    await waitFor(() =>
      expect(screen.getByText("Active namespaces").parentElement).toHaveTextContent(/^Active namespaces\s*1$/),
    );
  });

  it("tells two collections that look alike apart by their names", async () => {
    const twin = (namespace: string, title: string): MyNamespaceEntry => ({ ...MINE[1], namespace, title });
    vi.mocked(listMine).mockResolvedValueOnce([MINE[0], twin("col-a", "Research"), twin("col-b", "research ")]);
    vi.mocked(getMySharingSummary).mockResolvedValueOnce({
      ...SUMMARY,
      totals: { ...SUMMARY.totals, namespaces: 3 },
      namespaces: [row("team", "admin"), row("col-a", "writer"), row("col-b", "writer")],
      agent_namespaces: ["team", "col-a", "col-b"].map((namespace) => ({
        agent: "claude-code",
        namespace,
        writes: 1,
        recalls: 1,
        cross_namespace_recalls: 0,
      })),
    } as unknown as SharingSummary);
    useStore.setState({ myNamespaces: [MINE[0], twin("col-a", "Research"), twin("col-b", "research ")] });
    vi.mocked(listCollections).mockResolvedValueOnce({ collections: [], next_cursor: null });
    render(<Sharing />);
    const section = (await screen.findByRole("heading", { name: "Collections" })).closest("section") as HTMLElement;
    expect(await within(section).findByRole("button", { name: "Research (col-a)" })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: /^research\s+\(col-b\)$/ })).toBeInTheDocument();
    // Tool activity counts them as collections, and names them the same way.
    fireEvent.click(screen.getByRole("button", { name: "Also show 2 collections" }));
    expect(screen.getByText("across the 1 namespace and 2 collections you can read")).toBeInTheDocument();
    expect(screen.getByText("collection Research (col-a)")).toBeInTheDocument();
    expect(screen.getByText(/^collection research\s+\(col-b\)$/)).toBeInTheDocument();
  });
});
