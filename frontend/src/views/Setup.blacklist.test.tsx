import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, TokenEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { ScopeBadge } from "../components/ScopeBadge";
import { PRIVACY_FILTERS_MANAGED_BY_ADMIN, PRIVACY_FILTERS_READ_ONLY } from "./admin/Blacklist";
import { Setup } from "./Setup";

// Setup's "Never save" panel must not list the blacklist for every visitor: a
// viewer token that reads only code-clickhouse + tracker-clickhouse (no memory namespace)
// would draw a `blacklist.list` AUTH_INSUFFICIENT on every dashboard load. The panel asks
// `canCall` first: an unknown role sends nothing and reads one sentence; a granted viewer
// lists read-only; an admin lists and gets the add form and the remove buttons.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTokens: vi.fn(async (): Promise<TokenEntry[]> => []),
    listMine: vi.fn(async (): Promise<MyNamespaceEntry[]> => []),
    countMemories: vi.fn(async () => 12),
    listCodeRepos: vi.fn(async () => []),
    registerNamespace: vi.fn(async () => undefined),
    createToken: vi.fn(async () => ({
      token: "tok",
      name: "token-2026-01-01-abcdef",
      sa_subject: "sa:t",
      org: "dev",
      namespaces: ["reporter-public", "*"],
      ns_roles: {},
      roles: ["writer"],
      reads_everything: true,
      saves_to: "reporter-public",
      created_at: "2026-01-01T00:00:00",
      expires_at: null,
      restricted_namespaces: [],
      mcp_url: "http://localhost/mcp",
    })),
    listBlacklist: vi.fn(async () => ({
      rows: [
        {
          pattern_id: "p1",
          pattern: "sk-[A-Za-z0-9]{20,}",
          pattern_type: "regex",
          scope: "namespace",
          reason: "",
          created_by: "a@x",
          hit_count: 0,
          last_hit_at: null,
          created_at: "2026-09-16T00:00:00Z",
        },
      ],
    })),
  };
});
vi.mock("../api/webhooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/webhooks")>()),
  listWebhooks: vi.fn(async () => ({ webhooks: [] })),
}));
import { countMemories, listBlacklist, listMine, registerNamespace } from "../api";
import { listWebhooks } from "../api/webhooks";
import { sharedMine } from "../lib/sharedReads";

const ORG = "dev";
// A memory namespace the token has no row for.
const MEMORY_NS = "reporter-public";
const codeRow = (namespace: string, kind: "code" | "qa"): MyNamespaceEntry => ({
  namespace,
  role: "jwt",
  status: "active",
  visibility: "public",
  description: "",
  joined_at: null,
  is_new: false,
  kind,
});

const viewerNoMemory: WhoAmI = {
  authenticated: true,
  subject: "v@x",
  org: ORG,
  roles: ["VIEWER"],
  namespaces: ["code-clickhouse", "tracker-clickhouse"],
};

function seed(whoami: WhoAmI, mine: MyNamespaceEntry[] | null, namespace: string) {
  useStore.setState({
    authed: true,
    whoami,
    myNamespaces: mine,
    identity: { org: ORG, namespace, agent: "loom-ui" },
    view: "setup",
    memoriesVersion: 0,
    paletteRequest: null,
    paletteOpens: 0,
  });
}

const WAIT = { timeout: 5000 };
// The panel lives on the Memory tab; the Tokens tab is the landing one.
const openMemoryTab = async () => {
  await screen.findByRole("tab", { name: "Tokens" }, WAIT);
  await userEvent.click(screen.getByRole("tab", { name: "Memory" }));
};

describe("Setup's 'Never save' panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // The open tab rides the URL (`?tab=memory`); start each test on the landing tab.
    window.history.replaceState(null, "", "/settings");
    resetNamespaceRoleCache();
    // Stray fetches answer an empty object; nothing here is about them.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("a viewer token with only code and tracker namespaces never lists the blacklist and renders without error", async () => {
    seed(viewerNoMemory, [codeRow("code-clickhouse", "code"), codeRow("tracker-clickhouse", "qa")], MEMORY_NS);
    render(<Setup />);
    await openMemoryTab();
    expect(await screen.findByText(PRIVACY_FILTERS_MANAGED_BY_ADMIN, undefined, WAIT)).toBeInTheDocument();
    expect(listBlacklist).not.toHaveBeenCalled();
    // The same rule covers the checklist's memory.list total.
    expect(countMemories).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Substring to block")).not.toBeInTheDocument();
    expect(screen.queryByText(/Failed|Couldn't/)).not.toBeInTheDocument();
    expect(console.error).not.toHaveBeenCalled();
  });

  it("no namespace selected: still no request", async () => {
    seed(viewerNoMemory, [codeRow("code-clickhouse", "code")], "");
    render(<Setup />);
    await openMemoryTab();
    expect(await screen.findByText(PRIVACY_FILTERS_MANAGED_BY_ADMIN, undefined, WAIT)).toBeInTheDocument();
    expect(listBlacklist).not.toHaveBeenCalled();
    expect(countMemories).not.toHaveBeenCalled();
  });

  it("list_mine failed (unknown role): no request", async () => {
    seed(viewerNoMemory, null, MEMORY_NS);
    render(<Setup />);
    await openMemoryTab();
    expect(await screen.findByText(PRIVACY_FILTERS_MANAGED_BY_ADMIN, undefined, WAIT)).toBeInTheDocument();
    expect(listBlacklist).not.toHaveBeenCalled();
  });

  it("a viewer with a grant on the namespace lists once, read-only: no add form, no remove button", async () => {
    seed(viewerNoMemory, [{ ...codeRow(MEMORY_NS, "code"), kind: "memory", role: "viewer" }], MEMORY_NS);
    render(<Setup />);
    // Nothing is read for a tab that is not open: the list request waits for the Memory tab.
    await screen.findByRole("tab", { name: "Tokens" }, WAIT);
    expect(listBlacklist).not.toHaveBeenCalled();
    await openMemoryTab();
    await waitFor(() => expect(listBlacklist).toHaveBeenCalledTimes(1), WAIT);
    expect(await screen.findByText("sk-[A-Za-z0-9]{20,}", undefined, WAIT)).toBeInTheDocument();
    expect(screen.getByText(PRIVACY_FILTERS_READ_ONLY)).toBeInTheDocument();
    expect(screen.queryByLabelText("Substring to block")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove pattern/ })).not.toBeInTheDocument();
    expect(screen.queryByText(PRIVACY_FILTERS_MANAGED_BY_ADMIN)).not.toBeInTheDocument();
    expect(countMemories).toHaveBeenCalledTimes(1);
    // Webhooks are ADMIN: a viewer neither sees the section nor reads them.
    expect(screen.queryByRole("heading", { name: "Webhooks" })).not.toBeInTheDocument();
    expect(listWebhooks).not.toHaveBeenCalled();
  });

  it("an admin claim lists once and gets the add form and the remove button", async () => {
    const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
    seed(admin, [], MEMORY_NS);
    render(<Setup />);
    await openMemoryTab();
    await waitFor(() => expect(listBlacklist).toHaveBeenCalledTimes(1), WAIT);
    expect(vi.mocked(listBlacklist).mock.calls[0][0]).toEqual({ org: ORG, namespace: MEMORY_NS });
    expect(await screen.findByLabelText("Substring to block", undefined, WAIT)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Remove pattern sk-[A-Za-z0-9]{20,}" }, WAIT)).toBeInTheDocument();
    expect(screen.queryByText(PRIVACY_FILTERS_MANAGED_BY_ADMIN)).not.toBeInTheDocument();
    expect(screen.queryByText(PRIVACY_FILTERS_READ_ONLY)).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Webhooks" }, WAIT)).toBeInTheDocument();
  });

  it("drops the previous namespace's webhooks on a switch: the section is keyed by its namespace", async () => {
    const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
    const hook = {
      webhook_id: "w1",
      url: "https://hooks.example.com/loom",
      secret_ref: "LOOM_WEBHOOK_SECRET_DEV__CI",
      events: ["memory.written" as const],
      created_at: "2000-01-01 00:00:00",
      secret_present: true,
    };
    vi.mocked(listWebhooks).mockResolvedValueOnce({ webhooks: [hook] });
    seed(admin, [], MEMORY_NS);
    render(<Setup />);
    await openMemoryTab();
    expect(await screen.findByText(hook.url, undefined, WAIT)).toBeInTheDocument();
    vi.mocked(listWebhooks).mockImplementationOnce(() => new Promise(() => {}));
    act(() => useStore.setState({ identity: { org: ORG, namespace: "other-ns", agent: "loom-ui" } }));
    await waitFor(() => expect(screen.queryByText(hook.url)).toBeNull(), WAIT);
    // A fresh section for the new namespace, with no Remove from the old one.
    expect(listWebhooks).toHaveBeenLastCalledWith(
      expect.objectContaining({ namespace: "other-ns" }),
      expect.anything(),
    );
    expect(screen.queryByRole("button", { name: `Actions for ${hook.url}` })).toBeNull();
  });
});

describe("Setup's remembered connect choice", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/settings");
    resetNamespaceRoleCache();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("blocked storage (a throwing getItem) renders the default client instead of crashing", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
    seed(admin, [], MEMORY_NS);
    render(<Setup />);
    await userEvent.click(await screen.findByRole("button", { name: /Create token/ }, WAIT));
    const picker = await screen.findByLabelText(/^Connect/, undefined, WAIT);
    expect(picker).toHaveTextContent("Claude Code");
  });

  it("a stored id that names no client falls back to the default", async () => {
    localStorage.setItem("loom_last_mcp_client_v1", "not-a-client");
    const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
    seed(admin, [], MEMORY_NS);
    render(<Setup />);
    await userEvent.click(await screen.findByRole("button", { name: /Create token/ }, WAIT));
    expect(await screen.findByLabelText(/^Connect/, undefined, WAIT)).toHaveTextContent("Claude Code");
    localStorage.clear();
  });
});

describe("Setup's new-namespace button", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("refreshes the shared list_mine so every reader sees the new admin grant at once", async () => {
    const created: MyNamespaceEntry = { ...codeRow("robot-ns", "code"), kind: "memory", role: "admin" };
    const home: MyNamespaceEntry = { ...codeRow(MEMORY_NS, "code"), kind: "memory", role: "writer" };
    vi.mocked(listMine).mockResolvedValueOnce([home]).mockResolvedValue([home, created]);
    const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
    seed(admin, [], MEMORY_NS);
    render(<Setup />);
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    await userEvent.type(screen.getByLabelText("New namespace name"), "robot-ns");
    await userEvent.click(screen.getByRole("button", { name: /Create namespace/ }));
    await waitFor(() => expect(registerNamespace).toHaveBeenCalledTimes(1), WAIT);
    expect(vi.mocked(registerNamespace).mock.calls[0][1]).toMatchObject({ owner_email: "a@x" });
    await screen.findByLabelText("Read robot-ns", undefined, WAIT);
    const calls = vi.mocked(listMine).mock.calls.length;
    // The cache now holds the post-register rows: a later asker joins them, no new read.
    expect(await sharedMine(ORG)).toEqual([home, created]);
    expect(vi.mocked(listMine).mock.calls.length).toBe(calls);
  });

  it("a non-admin token's role chip on the new namespace reads admin at once", async () => {
    const created: MyNamespaceEntry = { ...codeRow("robot-ns", "code"), kind: "memory", role: "admin" };
    const home: MyNamespaceEntry = { ...codeRow(MEMORY_NS, "code"), kind: "memory", role: "writer" };
    vi.mocked(listMine).mockResolvedValueOnce([home]).mockResolvedValue([home, created]);
    const writer: WhoAmI = {
      authenticated: true,
      subject: "w@x",
      org: ORG,
      roles: ["WRITER"],
      namespaces: [MEMORY_NS],
    };
    // App's boot read has stored the pre-register rows, as it always does when authed.
    seed(writer, [home], MEMORY_NS);
    render(
      <>
        <ScopeBadge kind="namespace" value="robot-ns" />
        <Setup />
      </>,
    );
    await userEvent.click(await screen.findByRole("radio", { name: "Only these namespaces" }, WAIT));
    expect(screen.queryByText("Your role here: admin")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("New namespace name"), "robot-ns");
    await userEvent.click(screen.getByRole("button", { name: /Create namespace/ }));
    await waitFor(() => expect(registerNamespace).toHaveBeenCalledTimes(1), WAIT);
    expect(await screen.findByText("Your role here: admin", undefined, WAIT)).toBeInTheDocument();
    expect(useStore.getState().myNamespaces).toEqual([home, created]);
  });
});
