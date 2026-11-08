import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, TokenEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Setup } from "./Setup";

// Settings → Structure is for a memory namespace the token reads, and a namespace switch
// starts it afresh: Setup keys it by namespace, so no row, draft or dialog of the old one
// reaches the new one.

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  listTokens: vi.fn(async (): Promise<TokenEntry[]> => []),
  listMine: vi.fn(async (): Promise<MyNamespaceEntry[]> => []),
  countMemories: vi.fn(async () => 0),
  listCodeRepos: vi.fn(async () => []),
  listBlacklist: vi.fn(async () => ({ rows: [] })),
}));
vi.mock("../api/structure", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/structure")>()),
  listRecordSchemas: vi.fn(),
  getProfileSchema: vi.fn(async () => ({ found: false, json_schema: {}, updated_by: "", updated_at: null })),
  listOntology: vi.fn(async () => []),
  listCategories: vi.fn(async () => []),
}));
import { listRecordSchemas } from "../api/structure";

const ORG = "dev";
const row = (namespace: string, kind?: "code"): MyNamespaceEntry => ({
  namespace,
  role: "viewer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  ...(kind ? { kind } : {}),
});
const viewer: WhoAmI = { authenticated: true, subject: "v@x", org: ORG, roles: ["VIEWER"] };
const invoice = { schema_id: "s1", name: "invoice", description: "", status: "active", fields: [] };

function seed(namespace: string) {
  useStore.setState({
    authed: true,
    whoami: viewer,
    myNamespaces: [row("team-a"), row("team-b"), row("code-x", "code")],
    identity: { org: ORG, namespace, agent: "loom-ui" },
    view: "setup",
  });
}

describe("Settings → Structure", () => {
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
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("is not offered on a code namespace", async () => {
    seed("code-x");
    render(<Setup />);
    await screen.findByRole("tab", { name: "Tokens" });
    expect(screen.queryByRole("tab", { name: "Structure" })).toBeNull();
    expect(listRecordSchemas).not.toHaveBeenCalled();
  });

  it("drops the old namespace's rows on a switch, before the new ones answer", async () => {
    vi.mocked(listRecordSchemas)
      .mockResolvedValueOnce({ schemas: [invoice] })
      .mockImplementationOnce(() => new Promise(() => {}));
    seed("team-a");
    render(<Setup />);
    await userEvent.click(await screen.findByRole("tab", { name: "Structure" }));
    expect(await screen.findByText("invoice")).toBeInTheDocument();
    act(() => useStore.setState({ identity: { org: ORG, namespace: "team-b", agent: "loom-ui" } }));
    await waitFor(() => expect(screen.queryByText("invoice")).toBeNull());
    expect(listRecordSchemas).toHaveBeenLastCalledWith(
      expect.objectContaining({ namespace: "team-b" }),
      expect.anything(),
    );
  });
});
