import { cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, PendingApproval, TokenEntry, WhoAmI } from "../api";
import { useStore } from "./state";
import { resetNamespaceRoleCache } from "./namespaceRole";
import { invalidateShared } from "./sharedReads";
import { PendingApprovalsBell } from "../components/PendingApprovalsBell";
import { Sharing } from "../views/Sharing";
import { Setup } from "../views/Setup";

// `code.repos` is read on mount by the header bell, Setup's token form and Sharing; for a
// token with no code namespace that would be three silent 403s per page load (the
// transport gate targets the default code namespace). The read needs standing on a
// code-kind list_mine row or an admin claim.

const MINE: { rows: MyNamespaceEntry[] } = { rows: [] };

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => MINE.rows),
    listCodeRepos: vi.fn(async () => [
      { namespace: "code-clickhouse", org: "dev", repo: "ClickHouse/ClickHouse", branch: "master" },
    ]),
    listGrants: vi.fn(async () => ({ rows: [] })),
    listPendingApprovals: vi.fn(async () => ({ rows: [] })),
    getMySharingSummary: vi.fn(async () => {
      throw new Error("offline");
    }),
    listTokens: vi.fn(async (): Promise<TokenEntry[]> => []),
    countMemories: vi.fn(async () => 12),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
  };
});
import { listCodeRepos, listMine, listPendingApprovals } from "../api";

const row = (namespace: string, kind: MyNamespaceEntry["kind"], role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: "2026-09-16T00:00:00Z",
  is_new: false,
  kind,
});
const memoryOnly = [row("team-x", "memory", "writer")];
const withCode = [row("team-x", "memory", "writer"), row("code-clickhouse", "code", "jwt")];
const writer: WhoAmI = { authenticated: true, subject: "w@x", org: "dev", roles: ["WRITER"] };

function seed(mine: MyNamespaceEntry[]) {
  MINE.rows = mine;
  useStore.setState({
    authed: true,
    whoami: writer,
    myNamespaces: mine,
    identity: { org: "dev", namespace: "team-x", agent: "loom-ui" },
    view: "setup",
    memoriesVersion: 0,
    paletteRequest: null,
    paletteOpens: 0,
  });
}

const WAIT = { timeout: 5000 };
// The bell asks for repositories only when it has a code-namespace request to name; with
// standing AND such a request it still calls once. The "memory-only token" case keeps its
// empty inbox.
const codeRequest: PendingApproval = {
  namespace: "code-clickhouse",
  kind: "code",
  subject: "sa:bot",
  requested_role: "viewer",
  current_role: "viewer",
  pending_since: null,
};
const surfaces: Array<[string, () => JSX.Element, () => Promise<unknown>, (() => void)?]> = [
  [
    "the header bell",
    () => <PendingApprovalsBell />,
    () => waitFor(() => expect(listPendingApprovals).toHaveBeenCalled(), WAIT),
    () => {
      vi.mocked(listPendingApprovals).mockResolvedValueOnce({ rows: [codeRequest] });
    },
  ],
  ["Sharing", () => <Sharing />, () => waitFor(() => expect(listMine).toHaveBeenCalled(), WAIT)],
  ["Setup", () => <Setup />, () => screen.findByRole("tab", { name: "Tokens" }, WAIT)],
];

describe("code.repos is read only with standing on a code namespace", () => {
  beforeEach(() => {
    invalidateShared(); // Shared reads are memoised per page; each case is its own page
    vi.clearAllMocks();
    localStorage.clear();
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

  for (const [name, el, settled, arrange] of surfaces) {
    it(`${name}: a memory-only token issues no code.repos call`, async () => {
      seed(memoryOnly);
      arrange?.();
      render(el());
      await settled();
      // Let any chained read settle before asserting nothing was sent.
      await new Promise((r) => setTimeout(r, 50));
      expect(listCodeRepos).not.toHaveBeenCalled();
    });

    it(`${name}: a token with one code namespace still calls it once`, async () => {
      seed(withCode);
      arrange?.();
      render(el());
      await settled();
      await waitFor(() => expect(listCodeRepos).toHaveBeenCalledTimes(1), WAIT);
      expect(vi.mocked(listCodeRepos).mock.calls[0][0]).toBe("dev");
    });
  }
});
