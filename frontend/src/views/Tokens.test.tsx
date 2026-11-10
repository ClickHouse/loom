import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CodeRepoSummary, MyNamespaceEntry, TokenEntry, TokenCreated, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { invalidateShared } from "../lib/sharedReads";
import { Setup } from "./Setup";

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
  restricted_namespaces: [],
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
  };
});
import { createToken, listCodeRepos, listMine } from "../api";

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

  it("only these: the ticked list in search order with a role each", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-c" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read org/x branch main" }));
    // The server picks from the saved pick, which the page cannot see: the form states the rule.
    expect(screen.getByTestId("token-saves-listed")).toHaveTextContent(/The namespace you work in/);
    expect((await create()).namespaces).toEqual([
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
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
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

  it("only these: nothing is ticked at first, the page's namespace and the personal one included", async () => {
    const mine = [row("personal-a"), ...ROWS];
    vi.mocked(listMine).mockResolvedValue(mine);
    useStore.setState({ myNamespaces: mine, identity: { org: ORG, namespace: "ns-b", agent: "loom-ui" } });
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    const personal = await screen.findByRole("checkbox", { name: "Read personal-a" }, WAIT);
    expect(personal).not.toBeChecked();
    for (const ns of ["ns-a", "ns-b", "ns-c"]) {
      expect(screen.getByRole("checkbox", { name: `Read ${ns}` })).not.toBeChecked();
    }
    expect(screen.queryByRole("list", { name: "Search order" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
    expect(screen.getByText(/Tick the namespaces this token reads/)).toBeInTheDocument();
  });

  it("only these: every tick can be taken back, and then nothing is created", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-b" }));
    expect(screen.queryByRole("list", { name: "Search order" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
  });

  it("only these: the arrows reorder the list, and the token carries the order shown", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
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
    // And at the bottom its Move down is gone; focus moves to its Move up.
    const down = screen.getByRole("button", { name: "Move ns-c down" });
    await userEvent.click(down);
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

  it("only these: a role is picked in the ordered list and sent with the namespace", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
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
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Read ns-c" }, WAIT));
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-a" }));
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Reads only these, searched in this order: 1. ns-c, 2. ns-a. Saves to ns-a.",
    );
  });

  it("only these: a namespace created here joins the order last and clears the status", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
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

  it("only these: a row dragged onto another takes its place", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
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
    // Downward too, and a drop on its own place changes nothing.
    const now = () => within(screen.getByRole("list", { name: "Search order" })).getAllByRole("listitem");
    expect(screen.getByRole("status")).toHaveTextContent("ns-c moved to 1 of 3");
    fireEvent.dragStart(grip(now()[0]), { dataTransfer: data });
    fireEvent.dragOver(now()[2], { dataTransfer: data });
    fireEvent.drop(now()[2], { dataTransfer: data });
    fireEvent.dragStart(grip(now()[1]), { dataTransfer: data });
    fireEvent.drop(now()[1], { dataTransfer: data });
    expect(now().map((li) => li.textContent?.match(/ns-[abc]/)?.[0])).toEqual(["ns-a", "ns-b", "ns-c"]);
    fireEvent.dragStart(grip(now()[2]), { dataTransfer: data });
    fireEvent.dragOver(now()[0], { dataTransfer: data });
    fireEvent.drop(now()[0], { dataTransfer: data });
    expect((await create()).namespaces.map((n) => (typeof n === "string" ? n : n.namespace))).toEqual([
      "ns-c",
      "ns-a",
      "ns-b",
    ]);
  });

  it("only these with nothing writable says it saves nowhere", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    await screen.findByRole("checkbox", { name: "Read ns-a" }, WAIT);
    expect(screen.getByRole("button", { name: /Create token/ })).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "Read ns-c" }));
    expect(screen.getByTestId("token-saves-nowhere")).toHaveTextContent("Nowhere: it only reads.");
  });

  it("the result names what the token reads and where it saves, as the server minted it", async () => {
    vi.mocked(createToken).mockResolvedValueOnce(
      created({ namespaces: ["ns-a", "*", "code-secret"], restricted_namespaces: ["code-secret"] }),
    );
    render(<Setup />);
    await waitFor(() => expect(savesTo()).toHaveTextContent(/^ns-a/), WAIT);
    await create();
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Reads everything you belong to, including namespaces you join later, plus org/secret · main. Saves to ns-a.",
    );
    expect(screen.getByLabelText(/^Connect/)).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("claude-code-recommended")).toBeInTheDocument();
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
    expect(await screen.findByTestId("token-saves-nowhere", undefined, WAIT)).toHaveTextContent("Nowhere");
    expect((await create()).namespaces).toEqual(["*"]);
    expect(await screen.findByTestId("minted-token-scope", undefined, WAIT)).toHaveTextContent(
      "Read-only; calls that name no namespace read ns-a.",
    );
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
});
