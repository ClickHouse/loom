import { act, cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, ServiceAccountEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Setup } from "./Setup";

// The agent token's home namespace follows the page's namespace: a switch from A to B
// moves the picker and the mint to B, unless the person picked a namespace in this view.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listServiceAccounts: vi.fn(async (): Promise<ServiceAccountEntry[]> => []),
    listMine: vi.fn(async () => ["ns-a", "ns-b", "ns-c"].map(row)),
    countMemories: vi.fn(async () => 0),
    listCodeRepos: vi.fn(async () => []),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
    generateMcpConfig: vi.fn(async () => ({
      client: "claude-desktop" as const,
      config_path: { darwin: "", linux: "", win32: "" },
      config_key: "mcpServers",
      config: {},
      token: "tok",
      subject: "a@x",
      org: "dev",
      namespace: "ns-a",
      namespace_scope: "single" as const,
      mcp_url: "http://localhost/mcp",
    })),
  };
});
import { generateMcpConfig } from "../api";

const ORG = "dev";
function row(namespace: string): MyNamespaceEntry {
  return {
    namespace,
    role: "admin",
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
    kind: "memory",
  };
}
const admin: WhoAmI = { authenticated: true, subject: "a@x", org: ORG, roles: ["ADMIN"] };
const WAIT = { timeout: 5000 };

const switchTo = (namespace: string) =>
  act(() => {
    useStore.setState({ identity: { org: ORG, namespace, agent: "loom-ui" } });
  });
// Click UI's Select: the label names the trigger, which shows the picked option.
const picker = () => screen.getByLabelText(/Home namespace/);
const mintedNamespace = async () => {
  await userEvent.click(screen.getByRole("button", { name: /Generate token/ }));
  await waitFor(() => expect(generateMcpConfig).toHaveBeenCalledTimes(1), WAIT);
  return vi.mocked(generateMcpConfig).mock.calls[0][1]?.namespace;
};

describe("Setup's MCP home namespace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    window.history.replaceState(null, "", "/settings");
    resetNamespaceRoleCache();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 200 })),
    );
    useStore.setState({
      authed: true,
      whoami: admin,
      myNamespaces: ["ns-a", "ns-b", "ns-c"].map(row),
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

  it("follows a namespace switch into the picker and the mint", async () => {
    render(<Setup />);
    await waitFor(() => expect(picker()).toHaveTextContent(/^ns-a ·/), WAIT);
    switchTo("ns-b");
    await waitFor(() => expect(picker()).toHaveTextContent(/^ns-b ·/), WAIT);
    expect(await mintedNamespace()).toBe("ns-b");
  });

  it("keeps the namespace the person picked across a switch", async () => {
    render(<Setup />);
    await userEvent.click(await screen.findByLabelText(/Home namespace/, undefined, WAIT));
    await userEvent.click(await within(screen.getByRole("dialog")).findByText(/^ns-c/, undefined, WAIT));
    switchTo("ns-b");
    expect(picker()).toHaveTextContent(/^ns-c ·/);
    expect(await mintedNamespace()).toBe("ns-c");
  });

  it("announces each field's hint as its description", async () => {
    render(<Setup />);
    await waitFor(() => expect(picker()).toHaveTextContent(/^ns-a ·/), WAIT);
    expect(picker()).toHaveAccessibleDescription(/Where this client saves memories/);
    expect(screen.getAllByLabelText("Expires in (days)")[0]).toHaveAccessibleDescription(
      "Blank = 90 days. 0 = never expires (needs an SSO or `loom login` session). Max 3650, less if your own sign-in expires sooner.",
    );
  });
});
