import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CodeRepoSummary, MyNamespaceEntry, TokenEntry, TokenCreated, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { invalidateShared } from "../lib/sharedReads";
import { Setup } from "./Setup";
import { QuickConnect } from "./Tokens";

// One token form: what it reads (everything you belong to, or only the ticked
// namespaces), where it saves, a name and an expiry; one result dialog for every tool.

const created = (over: Partial<TokenCreated> = {}): TokenCreated => ({
  token: "tok-1",
  name: "token-2026-01-01-abcdef",
  sa_subject: "sa:token-2026-01-01-abcdef-123456",
  org: "dev",
  namespaces: ["ns-a", "*"],
  ns_roles: {},
  roles: ["writer"],
  reads_everything: true,
  saves_to: "ns-a",
  created_at: "2026-01-01T00:00:00",
  expires_at: null,
  mcp_url: "http://loom.test/mcp",
  ...over,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTokens: vi.fn(async (): Promise<TokenEntry[]> => []),
    listMine: vi.fn(async () => []),
    countMemories: vi.fn(async () => 0),
    listCodeRepos: vi.fn(async (): Promise<CodeRepoSummary[]> => []),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
    createToken: vi.fn(async () => created()),
    registerNamespace: vi.fn(async () => ({ registered: true })),
    listCollections: vi.fn(async () => ({ collections: [], next_cursor: null })),
  };
});
import { createToken, listCodeRepos, listCollections, listMine, listTokens } from "../api";

const ORG = "dev";
function row(namespace: string, over: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry {
  return {
    namespace,
    role: "admin",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
    ...over,
  };
}
const ROWS = [
  row("ns-a"),
  row("ns-b", { role: "writer" }),
  row("ns-c", { role: "viewer" }),
  row("code-x", { role: "viewer", kind: "code" }),
  row("code-secret", { role: "viewer", kind: "code" }),
];
const REPOS = [
  { namespace: "code-x", repo: "org/x", branch: "main", classification: "standard" },
  { namespace: "code-secret", repo: "org/secret", branch: "main", classification: "restricted" },
] as unknown as CodeRepoSummary[];
const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
const WAIT = { timeout: 5000 };

const switchTo = (namespace: string) =>
  act(() => {
    useStore.setState({ identity: { org: ORG, namespace, agent: "loom-ui" } });
  });
const savesTo = () => screen.getByLabelText(/Saves memories to/);
const create = async () => {
  await userEvent.click(screen.getByRole("button", { name: /Create token/ }));
  await waitFor(() => expect(createToken).toHaveBeenCalledTimes(1), WAIT);
  return vi.mocked(createToken).mock.calls[0][0];
};
// Click UI's Select opens its options in a popover dialog, the last one open.
const openOptions = async (label: RegExp) => {
  await userEvent.click(screen.getByLabelText(label));
  return (await screen.findAllByRole("dialog", undefined, WAIT)).at(-1) as HTMLElement;
};
const pickOption = async (label: RegExp, option: RegExp) => {
  await userEvent.click(await within(await openOptions(label)).findByText(option, undefined, WAIT));
};

describe("the token form", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState(null, "", "/settings");
    resetNamespaceRoleCache();
    // Each case answers list_mine and code.repos its own way.
    invalidateShared();
    vi.mocked(listMine).mockResolvedValue(ROWS);
    vi.mocked(listCodeRepos).mockResolvedValue(REPOS);
    vi.mocked(listCollections).mockResolvedValue({ collections: [], next_cursor: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: ROWS,
      identity: { org: ORG, namespace: "ns-a", agent: "loom-ui" },
      view: "setup",
      memoriesVersion: 0,
      paletteRequest: null,
      paletteOpens: 0,
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("by default reads everything and saves to the page's namespace, with no name", async () => {
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    expect(screen.getByRole("radio", { name: "Everything you belong to" })).toBeChecked();
    expect(await create()).toEqual({ name: undefined, namespaces: ["ns-a", "*"], ttl_days: 90 });
  });

  it("a namespace still awaiting approval is named as readable once approved", async () => {
    vi.mocked(listMine).mockResolvedValue([...ROWS, row("ns-asked", { status: "pending" })]);
    render(<Setup />);
    expect(await screen.findByText(/Awaiting approval: ns-asked\./, undefined, WAIT)).toBeInTheDocument();
  });

  it("saves to where the page goes, unless a namespace was picked here", async () => {
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    switchTo("ns-b");
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-b/), WAIT);
    await pickOption(/Saves memories to/, /^ns-a$/);
    switchTo("ns-b");
    expect(savesTo()).toHaveTextContent(/^ns-a/);
    // A namespace you only read is not offered as where it saves.
    const options = await openOptions(/Saves memories to/);
    expect(within(options).getByText("ns-b")).toBeInTheDocument();
    expect(within(options).queryByText("ns-c")).not.toBeInTheDocument();
  });

  it("a ticked restricted repository is named beside `*`, with the name typed", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("checkbox", { name: "Also read org/secret" }, WAIT));
    await userEvent.type(screen.getByLabelText("Name"), "laptop");
    expect(await create()).toMatchObject({ name: "laptop", namespaces: ["ns-a", "*", "code-secret"] });
  });

  it("only what you choose: the ticked list in search order with a role each", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-c" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read org/x branch main" }));
    // It saves to the first ticked namespace it writes, recorded in the token.
    expect(savesTo()).toHaveTextContent(/^ns-a/);
    const sent = await create();
    expect(sent.saves_to).toBe("ns-a");
    expect(sent.namespaces).toEqual([
      { namespace: "ns-a", role: "admin" },
      { namespace: "ns-c", role: "viewer" },
      { namespace: "ns-b", role: "writer" },
      { namespace: "code-x", role: "viewer" },
    ]);
  });

  it("a restricted repository is picked by repository and branch beside the ticked namespaces", async () => {
    // Two memory namespaces and a restricted repository with a release branch, held as
    // admin, viewer and viewer: the mint a CI token for one team and its private code needs.
    const owner = [
      row("team-private"),
      row("team-public", { role: "viewer" }),
      row("code-acme-private-26-8", { role: "viewer", kind: "code" }),
      row("code-acme-private", { role: "viewer", kind: "code" }),
    ];
    const repos = [
      {
        namespace: "code-acme-private",
        repo: "acme/private",
        branch: "master",
        classification: "restricted",
      },
      {
        namespace: "code-acme-private-26-8",
        repo: "acme/private",
        branch: "26.8",
        classification: "restricted",
      },
    ] as unknown as CodeRepoSummary[];
    vi.mocked(listMine).mockResolvedValue(owner);
    vi.mocked(listCodeRepos).mockResolvedValue(repos);
    useStore.setState({ myNamespaces: owner, identity: { org: ORG, namespace: "team-private", agent: "loom-ui" } });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    // The repository by its name, its branches beneath it, marked restricted; never its
    // namespace identifier among the memory namespaces.
    const repo = await screen.findByRole("checkbox", { name: "Read every branch of acme/private" }, WAIT);
    expect(repo.closest("div")?.parentElement).toHaveTextContent("restricted");
    // The default branch first, as the repository lists it, whatever order the grants came in.
    const branches = screen.getAllByRole("checkbox", { name: /^Read acme\/private branch / });
    expect(branches.map((b) => b.getAttribute("aria-label"))).toEqual([
      "Read acme/private branch master",
      "Read acme/private branch 26.8",
    ]);
    expect(screen.queryByRole("checkbox", { name: "Read code-acme-private" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: "Read team-private" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read team-public" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read acme/private branch master" }));
    expect((await create()).namespaces).toEqual([
      { namespace: "team-private", role: "admin" },
      { namespace: "team-public", role: "viewer" },
      { namespace: "code-acme-private", role: "viewer" },
    ]);
  }, 20_000);

  it("only what you choose: nothing is ticked at first, the page's namespace and the personal one included", async () => {
    const mine = [row("personal-a"), ...ROWS];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine, identity: { org: ORG, namespace: "ns-b", agent: "loom-ui" } });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    const personal = await screen.findByRole("checkbox", { name: "Read personal-a" }, WAIT);
    expect(personal).not.toBeChecked();
    for (const ns of ["ns-a", "ns-b", "ns-c"]) {
      expect(screen.getByRole("checkbox", { name: `Read ${ns}` })).not.toBeChecked();
    }
    expect(screen.queryByRole("list", { name: "Search order" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
    expect(screen.getByText(/Tick what this token reads/)).toBeInTheDocument();
  });

  it("only what you choose: each row shows your role, and the ordered list offers roles up to it", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await screen.findByRole("checkbox", { name: "Read ns-b" }, WAIT);
    const name = screen.getAllByText("ns-b").find((el) => el.tagName === "SPAN") as HTMLElement;
    expect(name.parentElement).toHaveTextContent("Your role: Writer");
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    const options = await openOptions(/^Role on ns-b$/);
    expect(within(options).getByText("Writer")).toBeInTheDocument();
    expect(within(options).getByText("Viewer")).toBeInTheDocument();
    expect(within(options).queryByText("Admin")).not.toBeInTheDocument();
  });

  it("only what you choose: one ticked namespace offers nothing to reorder", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    const order = screen.getByRole("list", { name: "Search order" });
    expect(order.querySelector("[draggable]")).toBeNull();
    expect(screen.queryByText(/to change the order/)).toBeNull();
    expect(screen.queryByText(/Number 1 is searched first/)).toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    expect(screen.getByText(/to change the order/)).toBeInTheDocument();
    // The reorder hint is the list's description, so a screen reader hears it on the list.
    expect(order).toHaveAccessibleDescription(/^Number 1 is searched first\. Drag a row by its grip/);
  });

  it("only what you choose: every tick can be taken back, and then nothing is created", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    expect(screen.queryByRole("list", { name: "Search order" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
  });

  it("only what you choose: the arrows reorder the list, and the token carries the order shown", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    for (const ns of ["ns-a", "ns-b", "ns-c"]) {
      await userEvent.click(await screen.findByRole("checkbox", { name: `Read ${ns}` }, WAIT));
    }
    const order = () =>
      within(screen.getByRole("list", { name: "Search order" }))
        .getAllByRole("listitem")
        .map((li) => li.textContent?.match(/ns-[abc]/)?.[0]);
    expect(order()).toEqual(["ns-a", "ns-b", "ns-c"]);
    // The first row cannot go up, the last cannot go down.
    expect(screen.getByRole("button", { name: "Move ns-a up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move ns-c down" })).toBeDisabled();
    const up = screen.getByRole("button", { name: "Move ns-c up" });
    await userEvent.click(up);
    expect(order()).toEqual(["ns-a", "ns-c", "ns-b"]);
    expect(up).toHaveFocus();
    await userEvent.click(up);
    expect(order()).toEqual(["ns-c", "ns-a", "ns-b"]);
    // At the top its Move up is gone; focus moves to its Move down, not to the page.
    expect(screen.getByRole("button", { name: "Move ns-c down" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("ns-c moved to 1 of 3");
    const down = screen.getByRole("button", { name: "Move ns-c down" });
    await userEvent.click(down);
    // A row moved down is re-inserted: focus still lands back on the arrow pressed.
    expect(screen.getByRole("button", { name: "Move ns-c down" })).toHaveFocus();
    // And at the bottom its Move down is gone; focus moves to its Move up.
    await userEvent.click(down);
    expect(order()).toEqual(["ns-a", "ns-b", "ns-c"]);
    expect(screen.getByRole("button", { name: "Move ns-c up" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("ns-c moved to 3 of 3");
    // A tick empties the line, so a later move with the same words is announced again.
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    expect(screen.getByRole("status")).toHaveTextContent("");
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("button", { name: "Move ns-c up" }));
    expect(screen.getByRole("status")).toHaveTextContent("ns-c moved to 1 of 3");
    expect((await create()).namespaces.map((n) => (typeof n === "string" ? n : n.namespace))).toEqual([
      "ns-c",
      "ns-a",
      "ns-b",
    ]);
  });

  it("only what you choose: a role is picked in the ordered list and sent with the namespace", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await pickOption(/^Role on ns-a$/, /^Viewer$/);
    expect((await create()).namespaces).toEqual([{ namespace: "ns-a", role: "viewer" }]);
  });

  it("the result of a list token names its namespaces in the order they are searched", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(
      created({
        namespaces: ["ns-c", "ns-a"],
        ns_roles: { "ns-c": "viewer", "ns-a": "admin" },
        reads_everything: false,
        saves_to: "ns-a",
      }),
    );
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-c" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Reads only these, searched in this order: 1. ns-c, 2. ns-a. Saves to ns-a.",
    );
  });

  it("only what you choose: a namespace created here joins the order last and clears the status", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("button", { name: "Move ns-b up" }));
    expect(screen.getByRole("status")).toHaveTextContent("ns-b moved to 1 of 2");
    vi.mocked(listMine).mockResolvedValue([...ROWS, row("nightly-bot")]);
    invalidateShared();
    await userEvent.type(screen.getByLabelText("New namespace name"), "nightly-bot");
    await userEvent.click(screen.getByRole("button", { name: "Create namespace" }));
    const order = screen.getByRole("list", { name: "Search order" });
    await waitFor(() => expect(within(order).getAllByRole("listitem")).toHaveLength(3), WAIT);
    expect(within(order).getAllByRole("listitem")[2]).toHaveTextContent("nightly-bot");
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("only what you choose on a touch screen: no grip to drag, and the hint names the arrows", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((q: string) => ({
        matches: q === "(pointer: coarse)",
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    const order = screen.getByRole("list", { name: "Search order" });
    expect(order.querySelector("[draggable]")).toBeNull();
    expect(screen.getByText(/Use a row's arrows to change the order/)).toBeInTheDocument();
  });

  it("only what you choose: a row dragged onto another takes its place", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    for (const ns of ["ns-a", "ns-b", "ns-c"]) {
      await userEvent.click(await screen.findByRole("checkbox", { name: `Read ${ns}` }, WAIT));
    }
    const items = within(screen.getByRole("list", { name: "Search order" })).getAllByRole("listitem");
    const data = { effectAllowed: "", setData: vi.fn(), getData: vi.fn(), setDragImage: vi.fn() };
    const grip = (li: HTMLElement) => li.querySelector("[draggable]") as HTMLElement;
    // Only the grip drags: a press on the row's role or arrows stays a press.
    expect(items[0]).not.toHaveAttribute("draggable");
    fireEvent.dragStart(grip(items[2]), { dataTransfer: data });
    fireEvent.dragOver(items[0], { dataTransfer: data });
    fireEvent.drop(items[0], { dataTransfer: data });
    const order = within(screen.getByRole("list", { name: "Search order" }))
      .getAllByRole("listitem")
      .map((li) => li.textContent?.match(/ns-[abc]/)?.[0]);
    expect(order).toEqual(["ns-c", "ns-a", "ns-b"]);
    expect(screen.getByRole("status")).toHaveTextContent("ns-c moved to 1 of 3");
    const now = () => within(screen.getByRole("list", { name: "Search order" })).getAllByRole("listitem");
    const names = () => now().map((li) => li.textContent?.match(/ns-[abc]/)?.[0]);
    // Downward too.
    fireEvent.dragStart(grip(now()[0]), { dataTransfer: data });
    fireEvent.dragOver(now()[2], { dataTransfer: data });
    fireEvent.drop(now()[2], { dataTransfer: data });
    expect(names()).toEqual(["ns-a", "ns-b", "ns-c"]);
    // A drop on its own place changes nothing.
    fireEvent.dragStart(grip(now()[1]), { dataTransfer: data });
    fireEvent.drop(now()[1], { dataTransfer: data });
    expect(names()).toEqual(["ns-a", "ns-b", "ns-c"]);
    fireEvent.dragStart(grip(now()[2]), { dataTransfer: data });
    fireEvent.dragOver(now()[0], { dataTransfer: data });
    fireEvent.drop(now()[0], { dataTransfer: data });
    expect((await create()).namespaces.map((n) => (typeof n === "string" ? n : n.namespace))).toEqual([
      "ns-c",
      "ns-a",
      "ns-b",
    ]);
  });

  it("only what you choose: a collection at Writer is not where it saves, and the hint says so", async () => {
    const mine = [...ROWS, row("col-team")];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read the collection col-team" }, WAIT));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent(
      "Nowhere by default: a call names where it saves.",
    );
    expect(
      screen.getByText(/^To give it a default, tick a memory namespace, not a collection, with Writer\.$/),
    ).toBeInTheDocument();
  });

  it("the token list never names a collection as where a token saves now", async () => {
    const mine = [row("team-y", { role: "viewer" }), row("col-team", { role: "writer" })];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    vi.mocked(listTokens).mockResolvedValue([
      {
        name: "bot",
        sa_subject: "sa:bot",
        namespaces: ["team-y", "col-team"],
        roles: ["writer"],
        ns_roles: { "team-y": "writer", "col-team": "writer" },
        saves_to: "team-y",
        yours: true,
        ttl_days: 90,
        created_by: "me",
        created_at: null,
        expires_at: null,
        revoked_at: null,
        revoked_by: "",
      },
    ]);
    try {
      render(<Setup />);
      expect(
        await screen.findByText(/saves nowhere by default now; can't write to team-y/, undefined, WAIT),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
    }
  });

  it("only what you choose: a repository at Writer alone only reads, since a repository is always read", async () => {
    const mine = ROWS.map((r) => (r.namespace === "code-x" ? { ...r, role: "writer" as const } : r));
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read org/x branch main" }, WAIT));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent("Nowhere: this token only reads.");
  });

  it("only what you choose with nothing writable says it saves nowhere", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT);
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-c" }));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent("Nowhere: this token only reads.");
    expect(await create()).not.toHaveProperty("saves_to");
  });

  it("only what you choose: where it saves is a ticked namespace it writes, in search order, and is sent", async () => {
    const mine = [...ROWS, row("col-team"), row("bench-x", { kind: "bench" })];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    for (const ns of ["ns-c", "col-team", "bench-x", "ns-b", "ns-a"]) {
      const name = ns === "col-team" ? "Read the collection col-team" : `Read ${ns}`;
      await userEvent.click(await screen.findByRole("checkbox", { name }, WAIT));
    }
    await userEvent.click(screen.getByRole("checkbox", { name: "Read org/x branch main" }));
    // The first it writes by default: ns-c is read-only, a collection, a bench corpus and a
    // repository are never where memories go.
    expect(savesTo()).toHaveTextContent(/^ns-b/);
    const options = await openOptions(/Saves memories to/);
    for (const ns of ["ns-c", "col-team", "bench-x", "org/x · main", "code-x"]) {
      expect(within(options).queryByText(ns)).not.toBeInTheDocument();
    }
    const text = options.textContent ?? "";
    expect(text.indexOf("ns-b")).toBeLessThan(text.indexOf("ns-a"));
    await userEvent.click(within(options).getByText("ns-a"));
    // A move does not move the pick.
    await userEvent.click(screen.getByRole("button", { name: "Move ns-a up" }));
    expect(savesTo()).toHaveTextContent(/^ns-a/);
    expect((await create()).saves_to).toBe("ns-a");
  });

  it("only what you choose: with no pick the target is the first in search order, and follows a move", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-b" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    expect(savesTo()).toHaveTextContent(/^ns-b/);
    await userEvent.click(screen.getByRole("button", { name: "Move ns-a up" }));
    expect(savesTo()).toHaveTextContent(/^ns-a/);
    // Nothing was picked, so nothing was taken away.
    expect(screen.queryByText(/now: /)).toBeNull();
    expect((await create()).saves_to).toBe("ns-a");
  });

  it("only what you choose: a pick that a tick or a role takes away gives way to the first, and says why", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await pickOption(/Saves memories to/, /^ns-b$/);
    await pickOption(/^Role on ns-b$/, /^Viewer$/);
    expect(savesTo()).toHaveTextContent(/^ns-a/);
    expect(savesTo()).toHaveAccessibleDescription("Saves to ns-a now: it can no longer write to ns-b.");
    // A move says where it saves itself: the note about the dropped pick goes.
    await userEvent.click(screen.getByRole("button", { name: "Move ns-b up" }));
    expect(screen.queryByText(/now: /)).toBeNull();
    await pickOption(/^Role on ns-b$/, /^Writer$/);
    await pickOption(/Saves memories to/, /^ns-b$/);
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    expect(savesTo()).toHaveAccessibleDescription("Saves to ns-a now: ns-b is no longer ticked.");
    // The hint is read out when it changes.
    expect(screen.getByText("Saves to ns-a now: ns-b is no longer ticked.")).toHaveAttribute("aria-live", "polite");
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent("Nowhere: this token only reads.");
    // ns-a was only the default, never picked: nothing was taken away, so no note.
    expect(screen.queryByText(/now: /)).toBeNull();
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    await pickOption(/Saves memories to/, /^ns-a$/);
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    expect(screen.getByText("Saves nowhere now: ns-a is no longer ticked.")).toBeInTheDocument();
  });

  it("everything: a collection is never offered as where it saves", async () => {
    const mine = [...ROWS, row("col-team")];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    const options = await openOptions(/Saves memories to/);
    expect(within(options).queryByText("col-team")).not.toBeInTheDocument();
  });

  it("the result names what the token reads and where it saves, as the server minted it", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["ns-a", "*", "code-secret"] }));
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Reads everything you belong to, including namespaces you join later, plus org/secret · main. Saves to ns-a.",
    );
    expect(screen.getByLabelText(/^Connect/)).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("claude-code-recommended")).toBeInTheDocument();
  });

  it("the result of a `*` token whose claim names a collection names it, and no other", async () => {
    const rows = [...ROWS, row("col-r", { role: "viewer", kind: "collection", title: "Research" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["ns-a", "*", "col-r"] }));
    render(<Setup />);
    await screen.findByTestId("token-read-scope", undefined, WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      /^Reads everything you belong to, including namespaces you join later, plus the collection Research, but no other collection\./,
    );
  });

  it("says so, and creates nothing, when your namespaces cannot be read", async () => {
    vi.mocked(listMine).mockRejectedValue(new Error("down"));
    render(<Setup />);
    expect(await screen.findByText(/Could not load your namespaces/, undefined, WAIT)).toBeInTheDocument();
    expect(screen.getByTestId("token-saves-loading")).toHaveTextContent("Unknown");
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
  });

  it("a read-only list token says so, and a script gets the export line and the Python snippet", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(
      created({
        namespaces: ["ns-c"],
        ns_roles: { "ns-c": "viewer" },
        reads_everything: false,
        saves_to: "ns-c",
      }),
    );
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Reads only ns-c. Read-only; calls that name no namespace read ns-c.",
    );
    // A token for only some namespaces opens on the script setup.
    expect(screen.getByLabelText(/^Connect/)).toHaveTextContent("A script");
    expect(screen.getByText('export LOOM_API_KEY="tok-1"')).toBeInTheDocument();
    expect(screen.getByText(/namespace="ns-c"/)).toBeInTheDocument();
    await pickOption(/^Connect/, /^Cursor$/);
    expect(localStorage.getItem("loom_last_mcp_client_v1")).toBe("cursor");
  });

  it("a script for a list token that saves nowhere reads its first memory namespace", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(
      created({
        namespaces: ["code-secret", "ns-c"],
        ns_roles: { "code-secret": "viewer", "ns-c": "viewer" },
        reads_everything: false,
        saves_to: "",
      }),
    );
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByText(/namespace="ns-c"/, undefined, WAIT)).toBeInTheDocument();
  });

  it("a token that reads everything with a viewer role of its own is read-only", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(created({ roles: ["viewer"] }));
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Read-only; calls that name no namespace read ns-a.",
    );
  });

  it("a token that reads everything of someone who writes nowhere is read-only", async () => {
    const viewerRows = ROWS.map((r) => ({ ...r, role: "viewer" as const }));
    vi.mocked(listMine).mockResolvedValue(viewerRows);
    useStore.setState({ myNamespaces: viewerRows });
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["*"], saves_to: "ns-a" }));
    render(<Setup />);
    const nowhere = await screen.findByTestId("token-saves-nowhere", undefined, WAIT);
    expect(nowhere).toHaveTextContent("Nowhere: this token only reads.");
    expect(nowhere).toHaveTextContent("You cannot write to any namespace.");
    expect((await create()).namespaces).toEqual(["*"]);
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Read-only; calls that name no namespace read ns-a.",
    );
  });

  it("everything: one who writes only a collection gets a read-only token, its default named as read", async () => {
    // Viewer on ns-a, writer on a collection: a `*` claim names no collection, so the token
    // reads and writes none.
    const rows = [row("ns-a", { role: "viewer" }), row("col-team", { role: "writer" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["*"], saves_to: "ns-a" }));
    render(<Setup />);
    const nowhere = await screen.findByTestId("token-saves-nowhere", undefined, WAIT);
    expect(nowhere).toHaveTextContent("Nowhere: this token only reads.");
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Read-only; calls that name no namespace read ns-a.",
    );
  });

  it("names a list token's read default as read when it writes only a collection", async () => {
    const rows = [row("ns-c", { role: "viewer" }), row("col-team", { role: "writer" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    vi.mocked(createToken).mockResolvedValueOnce(
      created({
        namespaces: ["ns-c", "col-team"],
        ns_roles: { "ns-c": "viewer", "col-team": "writer" },
        roles: [],
        reads_everything: false,
        saves_to: "ns-c",
      }),
    );
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-c" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read the collection col-team" }));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent(
      "Nowhere by default: a call names where it saves.",
    );
    expect(await create()).not.toHaveProperty("saves_to");
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Nowhere by default; calls that name no namespace read ns-c.",
    );
  });

  it("the token list says nothing it cannot know while your namespaces load", async () => {
    useStore.setState({ myNamespaces: null });
    vi.mocked(listMine).mockImplementation(() => new Promise(() => undefined));
    vi.mocked(listTokens).mockResolvedValue([
      {
        name: "bot",
        sa_subject: "sa:bot",
        namespaces: ["ns-a"],
        roles: ["writer"],
        ns_roles: { "ns-a": "writer" },
        saves_to: "ns-a",
        yours: true,
        ttl_days: 90,
        created_by: "me",
        created_at: null,
        expires_at: null,
        revoked_at: null,
        revoked_by: "",
      },
    ]);
    try {
      render(<Setup />);
      expect(
        await screen.findByText(/reads only ns-a \(writer\) · saves to ns-a/, undefined, WAIT),
      ).toBeInTheDocument();
      expect(screen.queryByText(/read-only now/)).toBeNull();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
      vi.mocked(listMine).mockResolvedValue(ROWS);
    }
  });

  it("says when calls must name a namespace, and when it cannot say where it saves", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(created({ saves_to: "" }));
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Each call must name a namespace.",
    );
  });

  it("everything: reads no collection, and says how a token reads one", async () => {
    const rows = [...ROWS, row("col-r", { role: "writer", kind: "collection", title: "Research" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    render(<Setup />);
    expect(
      await screen.findByText(
        "Every namespace and repository you are a member of, including ones you join later, at your role in each.",
        undefined,
        WAIT,
      ),
    ).toBeInTheDocument();
    const scope = await screen.findByTestId("token-read-scope", undefined, WAIT);
    expect(scope).not.toHaveTextContent(/col-r|Research/);
    expect(await screen.findByTestId("token-read-scope-collections", undefined, WAIT)).toHaveTextContent(
      "This token reads no collection. To include a shared collection, pick “Only what you choose” and tick it.",
    );
  });

  it("only what you choose: one of two look-alike collections, ticked alone, keeps its name in the order and the result", async () => {
    const mine = [
      ...ROWS,
      row("col-a", { role: "writer", kind: "collection", title: "Research" }),
      row("col-b", { role: "writer", kind: "collection", title: "research " }),
    ];
    vi.mocked(listMine).mockResolvedValue(mine);
    vi.mocked(listCollections).mockResolvedValue({
      collections: [
        { namespace: "col-a", parent: "", personal: false, role: "writer", title: "Research" },
        { namespace: "col-b", parent: "", personal: false, role: "writer", title: "research " },
      ],
      next_cursor: null,
    });
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read the collection Research (col-a)" }, WAIT));
    const order = within(screen.getByRole("list", { name: "Search order" }));
    expect(order.getAllByRole("listitem")[0]).toHaveTextContent(/^1Research \(col-a\)/);
    vi.mocked(createToken).mockResolvedValueOnce(
      created({ namespaces: ["col-a"], reads_everything: false, saves_to: null }),
    );
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      /^Reads only the collection Research \(col-a\)\./,
    );
  });

  it("only what you choose: two collections that look alike differ by name in the form, the search order and the result", async () => {
    const mine = [
      ...ROWS,
      // Both at the top, so their places match too; one title differs only in case and spacing.
      row("col-a", { role: "writer", kind: "collection", title: "Research" }),
      row("col-b", { role: "writer", kind: "collection", title: "research " }),
    ];
    vi.mocked(listMine).mockResolvedValue(mine);
    vi.mocked(listCollections).mockResolvedValue({
      collections: [
        { namespace: "col-a", parent: "", personal: false, role: "writer", title: "Research" },
        { namespace: "col-b", parent: "", personal: false, role: "writer", title: "research " },
      ],
      next_cursor: null,
    });
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    const a = await screen.findByRole("checkbox", { name: "Read the collection Research (col-a)" }, WAIT);
    const b = screen.getByRole("checkbox", { name: /^Read the collection research\s+\(col-b\)$/ });
    await userEvent.click(a);
    await userEvent.click(b);
    const order = within(screen.getByRole("list", { name: "Search order" }));
    expect(order.getByRole("button", { name: "Move Research (col-a) down" })).toBeInTheDocument();
    expect(order.getByRole("button", { name: /^Move research\s+\(col-b\) up$/ })).toBeInTheDocument();
    // The result names them the same way.
    vi.mocked(createToken).mockResolvedValueOnce(
      created({ namespaces: ["col-a", "col-b"], reads_everything: false, saves_to: null }),
    );
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      /1\. the collection Research \(col-a\), 2\. the collection research\s+\(col-b\)\./,
    );
  });

  it("only what you choose: a collection is offered under Collections by its title and place, and sends its name", async () => {
    const mine = [...ROWS, row("col-r", { role: "writer", kind: "collection", title: "Research" })];
    vi.mocked(listMine).mockResolvedValue(mine);
    vi.mocked(listCollections).mockResolvedValue({
      collections: [
        { namespace: "col-lib", parent: "", personal: false, role: "viewer", title: "Papers" },
        { namespace: "col-r", parent: "col-lib", personal: false, role: "writer", title: "Research" },
        // Read through its parent, with no grant of its own: the mint would refuse it.
        { namespace: "col-inherited", parent: "col-lib", personal: false, role: "viewer", title: "Inherited" },
      ],
      next_cursor: null,
    });
    useStore.setState({ myNamespaces: mine });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    const box = await screen.findByRole("checkbox", { name: "Read the collection Research" }, WAIT);
    const group = screen.getByText("Collections", { selector: ".section-label" }).parentElement as HTMLElement;
    expect(within(group).getByText("Research")).toBeInTheDocument();
    // Its place is where it sits, its own title shown once, beside it.
    await waitFor(() => expect(within(group).getByText("Library › Papers")).toBeInTheDocument(), WAIT);
    // The place describes the checkbox, so two collections of one title differ by ear too.
    expect(box).toHaveAccessibleDescription("Library › Papers");
    expect(within(group).getAllByText(/Research/)).toHaveLength(1);
    // Never under the namespaces, never by its address, and none it reads only through a parent.
    expect(screen.queryByRole("checkbox", { name: "Read col-r" })).toBeNull();
    expect(screen.queryByText("col-r")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /Inherited|col-inherited/ })).toBeNull();
    // Its address is in a tooltip the keyboard reaches: the title is the next tab stop.
    box.focus();
    await userEvent.tab();
    expect(document.activeElement).toHaveTextContent("Research");
    // Its ring is drawn whole and its place stays visible with a long title: focus-rings.spec.ts.
    expect(await screen.findByRole("tooltip")).toHaveTextContent("col-r");
    await userEvent.click(box);
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    // In the search order it is a row of its own, named by its title alone.
    const order = within(screen.getByRole("list", { name: "Search order" }));
    expect(order.getAllByRole("listitem")[0]).toHaveTextContent(/^1Research/);
    expect(order.queryByText(/col-r|the collection/)).toBeNull();
    expect(order.getByRole("button", { name: /^Role on Research/ })).toBeInTheDocument();
    await userEvent.click(order.getByRole("button", { name: "Move Research down" }));
    expect(screen.getByRole("status")).toHaveTextContent("Research moved to 2 of 2");
    expect((await create()).namespaces).toEqual([
      { namespace: "ns-a", role: "admin" },
      { namespace: "col-r", role: "writer" },
    ]);
  });

  it("the token list names a collection by its title", async () => {
    const mine = [row("team"), row("col-r", { role: "viewer", kind: "collection", title: "Research" })];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine });
    vi.mocked(listTokens).mockResolvedValue([
      {
        name: "bot",
        sa_subject: "sa:bot",
        namespaces: ["col-r", "team"],
        roles: [],
        ns_roles: { "col-r": "viewer", team: "viewer" },
        saves_to: undefined,
        yours: true,
        ttl_days: 90,
        created_by: "me",
        created_at: null,
        expires_at: null,
        revoked_at: null,
        revoked_by: "",
      },
    ]);
    try {
      render(<Setup />);
      expect(
        await screen.findByText(/reads only the collection Research, team \(viewer\)/, undefined, WAIT),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
    }
  });

  it("the token list tells two collections of one title apart by their names", async () => {
    const mine = [
      row("col-a", { role: "viewer", kind: "collection", title: "Research" }),
      row("col-b", { role: "viewer", kind: "collection", title: "Research" }),
    ];
    vi.mocked(listMine).mockResolvedValue(mine);
    vi.mocked(listCollections).mockResolvedValue({
      collections: [
        { namespace: "col-legal", parent: "", personal: false, role: "viewer", title: "Legal" },
        { namespace: "col-a", parent: "", personal: false, role: "viewer", title: "Research" },
        { namespace: "col-b", parent: "col-legal", personal: false, role: "viewer", title: "Research" },
      ],
      next_cursor: null,
    });
    useStore.setState({ myNamespaces: mine });
    vi.mocked(listTokens).mockResolvedValue([
      {
        name: "bot",
        sa_subject: "sa:bot",
        namespaces: ["col-a", "col-b"],
        roles: [],
        ns_roles: { "col-a": "viewer", "col-b": "viewer" },
        saves_to: undefined,
        yours: true,
        ttl_days: 90,
        created_by: "me",
        created_at: null,
        expires_at: null,
        revoked_at: null,
        revoked_by: "",
      },
    ]);
    try {
      render(<Setup />);
      expect(
        await screen.findByText(
          /reads only the collection Research \(col-a\), the collection Research \(col-b\) \(viewer\)/,
          undefined,
          WAIT,
        ),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
    }
  });

  it("everything: a writer only on a tracker record reads, in the form, the result and the list", async () => {
    const rows = [row("ns-a", { role: "viewer" }), row("tracker-x", { role: "writer", kind: "qa" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    // No token yet, so the form is open; the one created is then listed.
    vi.mocked(listTokens)
      .mockResolvedValueOnce([])
      .mockResolvedValue([
        {
          name: "bot",
          sa_subject: "sa:bot",
          namespaces: ["*"],
          roles: ["writer"],
          ns_roles: {},
          saves_to: undefined,
          yours: true,
          ttl_days: 90,
          created_by: "me",
          created_at: null,
          expires_at: null,
          revoked_at: null,
          revoked_by: "",
        },
      ]);
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["*"], saves_to: "ns-a" }));
    try {
      render(<Setup />);
      const nowhere = await screen.findByTestId("token-saves-nowhere", undefined, WAIT);
      expect(nowhere).toHaveTextContent("Nowhere: this token only reads.");
      await create();
      expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
        "Read-only; calls that name no namespace read ns-a.",
      );
      expect(
        await screen.findByText(/reads everything you belong to · read-only/, undefined, WAIT),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
    }
  }, 20_000);

  it("everything: a member of a collection is told, in the result and the list, that the token reads none", async () => {
    const rows = [
      row("ns-a", { role: "writer" }),
      row("col-r", { role: "viewer", kind: "collection", title: "Research" }),
    ];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    const token = {
      name: "bot",
      sa_subject: "sa:bot",
      namespaces: ["*"],
      roles: ["writer"],
      ns_roles: {},
      saves_to: undefined,
      yours: true,
      ttl_days: 90,
      created_by: "me",
      created_at: null,
      expires_at: null,
      revoked_at: null,
      revoked_by: "",
    };
    vi.mocked(listTokens).mockResolvedValueOnce([]).mockResolvedValue([token]);
    vi.mocked(createToken).mockResolvedValueOnce(created({ namespaces: ["*"], saves_to: "ns-a" }));
    try {
      render(<Setup />);
      expect(await screen.findByTestId("token-read-scope-collections", undefined, WAIT)).toBeInTheDocument();
      await create();
      expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
        /^Reads everything you belong to, including namespaces you join later, but no collection\./,
      );
      expect(
        await screen.findByText(/reads everything you belong to, but no collection · /, undefined, WAIT),
      ).toBeInTheDocument();
    } finally {
      vi.mocked(listTokens).mockResolvedValue([]);
    }
  }, 20_000);

  it("only what you choose: an account that holds only collections gets no empty Namespaces list", async () => {
    const rows = [row("col-r", { role: "writer", kind: "collection", title: "Research" })];
    vi.mocked(listMine).mockResolvedValue(rows);
    vi.mocked(listCodeRepos).mockResolvedValue([]);
    useStore.setState({ myNamespaces: rows });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only what you choose" }, WAIT));
    await screen.findByRole("checkbox", { name: "Read the collection Research" }, WAIT);
    const reads = screen.getByRole("group", { name: "What this token reads" });
    expect(within(reads).queryByText("Namespaces", { selector: ".section-label" })).toBeNull();
    expect(reads.querySelectorAll(".divide-y")).toHaveLength(1);
  });

  it("everything: says nothing of collections to someone who reads none, and does to one who reads one through a group", async () => {
    vi.mocked(listMine).mockResolvedValue(ROWS);
    useStore.setState({ myNamespaces: ROWS });
    vi.mocked(listCollections).mockResolvedValue({ collections: [], next_cursor: null });
    const first = render(<Setup />);
    await screen.findByTestId("token-read-scope", undefined, WAIT);
    // Never drawn for someone who reads none: not while the collection read is out, nor
    // once its answer (none) is drawn.
    await waitFor(() => expect(listCollections).toHaveBeenCalled());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.queryByTestId("token-read-scope-collections")).toBeNull();
    first.unmount();
    // Reached through a group: no list_mine row, but collection.list names it.
    invalidateShared();
    vi.mocked(listCollections).mockResolvedValue({
      collections: [{ namespace: "col-team", parent: "", personal: false, role: "viewer", title: "Team" }],
      next_cursor: null,
    });
    render(<Setup />);
    expect(await screen.findByTestId("token-read-scope-collections", undefined, WAIT)).toHaveTextContent(
      /^This token reads no collection\.$/,
    );
  });

  it("everything: says nothing of collections to one who reaches a collection only through its creator", async () => {
    // A client token's rows read through its creator: the collection gate admits none of them.
    const rows = [...ROWS, row("col-team", { role: "writer", kind: "collection", inherited: true })];
    vi.mocked(listMine).mockResolvedValue(rows);
    useStore.setState({ myNamespaces: rows });
    render(<Setup />);
    await screen.findByTestId("token-read-scope", undefined, WAIT);
    await waitFor(() => expect(listCollections).toHaveBeenCalled());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.queryByTestId("token-read-scope-collections")).toBeNull();
  });

  it("in a checklist step, says a token was made and is done once its setup dialog closes", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(created());
    const minted = vi.fn();
    const closed = vi.fn();
    render(<QuickConnect onMinted={minted} onClose={closed} />);
    await screen.findByRole("region", { name: "New token" }, WAIT);
    await create();
    expect(minted).toHaveBeenCalledTimes(1);
    const dialog = await screen.findByRole("dialog", { name: "Token created" }, WAIT);
    expect(closed).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(dialog).not.toBeInTheDocument(), WAIT);
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it("in a checklist step, takes focus into its form and is closed by its Cancel", async () => {
    const onClose = vi.fn();
    render(<QuickConnect onMinted={() => {}} onClose={onClose} />);
    const form = await screen.findByRole("region", { name: "New token" }, WAIT);
    // Its title, not the Cancel that comes first; inside the step's h3 it is an h4.
    const title = within(form).getByRole("heading", { name: "New token", level: 4 });
    await waitFor(() => expect(title).toHaveFocus(), WAIT);
    await userEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
