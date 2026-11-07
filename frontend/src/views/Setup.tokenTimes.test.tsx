import { cleanup, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TokenEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { Setup } from "./Setup";

// The server answers a token's times as naive UTC ("2026-10-19T10:00:00", no zone). Read as
// local east of UTC, a token minted a minute ago would look two hours old and stranded. The
// clock is pinned to July, when Berlin is two hours ahead.

const DAY = 24 * 3600 * 1000;
// A naive UTC time `ms` from now, as the server spells it.
const naive = (ms: number) => new Date(Date.now() + ms).toISOString().slice(0, 19);
const token = (over: Partial<TokenEntry>): TokenEntry => ({
  name: "tok",
  sa_subject: "sa-tok",
  namespaces: ["ns-a"],
  roles: ["writer"],
  ttl_days: 15,
  created_by: "a@x",
  created_at: naive(-60 * 1000),
  expires_at: null,
  revoked_at: null,
  revoked_by: "",
  last_used_at: null,
  ...over,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTokens: vi.fn(
      async (): Promise<TokenEntry[]> => [
        // Minted a minute ago, never used, expiring an hour past the 14-day warning.
        token({ name: "fresh", sa_subject: "sa-fresh", expires_at: naive(14 * DAY + 3600 * 1000) }),
        // Last used an hour inside the 7-day active window.
        token({
          name: "used",
          sa_subject: "sa-used",
          created_at: naive(-30 * DAY),
          last_used_at: naive(-7 * DAY + 3600 * 1000),
        }),
      ],
    ),
    listMine: vi.fn(async () => []),
    countMemories: vi.fn(async () => 0),
    listCodeRepos: vi.fn(async () => []),
    listBlacklist: vi.fn(async () => ({ rows: [] })),
  };
});

const admin: WhoAmI = { authenticated: true, subject: "a@x", org: "dev", roles: ["ADMIN"] };
const WAIT = { timeout: 5000 };

describe("Setup's token times, east of UTC", () => {
  beforeEach(() => {
    vi.stubEnv("TZ", "Europe/Berlin");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-07-15T10:00:00Z"));
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
      myNamespaces: [],
      identity: { org: "dev", namespace: "ns-a", agent: "loom-ui" },
      view: "setup",
    });
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null });
  });

  it("reads a just-minted token as new, and counts tokens as their badges show them", async () => {
    render(<Setup />);
    expect(await screen.findByText("fresh", undefined, WAIT)).toBeInTheDocument();
    expect(screen.queryByText(/Not connecting\?/)).not.toBeInTheDocument();
    expect(screen.getByText(/waiting for first use/)).toHaveTextContent(
      "1 active · 1 waiting for first use · 0 idle (>7d)",
    );
    expect(screen.queryByText(/in the next 14 days/)).not.toBeInTheDocument();
    expect(screen.getAllByText("Waiting…")).toHaveLength(1);
    expect(screen.getAllByText("Connected")).toHaveLength(1);
  });
});
