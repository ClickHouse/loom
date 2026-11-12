/**
 * The Health tab's coverage table and index-error list read beside the status row. A read
 * that fails ends its loading state with a stated error; it never leaves a loader up.
 */
import { act, cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/errors";
import type { CodeRepoSummary, MyNamespaceEntry } from "../api";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { useStore } from "../lib/state";
import { invalidateShared } from "../lib/sharedReads";
import { HealthTab } from "./Code";

const ST = {
  org: "dev",
  namespace: "code-app",
  repo: "acme/app",
  branch: "main",
  provider: "github",
  profile: "generic",
  git_head: "abc123def456",
  clone_head: "abc123def456",
  fast_tier_lag: 0,
  counts: {},
  path_prefixes: [],
  compiler_tiers: [],
  pr_cursor: "",
  state: {},
  workers: {},
};

const codeCoverage = vi.fn();
const codeIndexErrors = vi.fn();
const codeRecentCommits = vi.fn(async () => ({ rows: [] }));
const listMine = vi.fn(async (..._a: unknown[]): Promise<MyNamespaceEntry[]> => []);
const codeRefresh = vi.fn(async (..._a: unknown[]) => ({ already_running: false }));
const sharedIndexStatus = vi.fn(async (): Promise<typeof ST> => ST);
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    codeCoverage: (...a: unknown[]) => codeCoverage(...a),
    codeIndexErrors: (...a: unknown[]) => codeIndexErrors(...a),
    codeRecentCommits: (...a: unknown[]) => codeRecentCommits(...(a as [])),
    listCostRollup: async () => ({ rows: [] }),
    listMine: (...a: unknown[]) => listMine(...a),
    codeRefresh: (...a: unknown[]) => codeRefresh(...a),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedIndexStatus: () => sharedIndexStatus() };
});

afterEach(() => cleanup());

const REPO = { repo: "acme/app", branch: "main", namespace: "code-app" } as CodeRepoSummary;

describe("HealthTab: failed reads", () => {
  it("states that coverage, the index errors and the recent commits could not be read", async () => {
    codeCoverage.mockRejectedValue(new Error("boom"));
    codeIndexErrors.mockRejectedValue(new Error("boom"));
    codeRecentCommits.mockRejectedValueOnce(new Error("boom"));
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
    // Each line says why, not only that it failed.
    expect(await screen.findByText("Couldn't load coverage: boom")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load coverage: boom")).toHaveAttribute("role", "alert");
    expect(screen.getByText("Couldn't load the index errors: boom")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load the recent commits: boom")).toBeInTheDocument();
    expect(screen.queryByText("No commits recorded yet.")).toBeNull();
  });
});

describe("HealthTab: a failed poll", () => {
  it("keeps the last status on screen and says it could not refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      codeCoverage.mockResolvedValue(null);
      codeIndexErrors.mockResolvedValue({ rows: [], total: 0 });
      codeRecentCommits.mockResolvedValue({ rows: [] });
      render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
      expect(await screen.findByText(/generic profile/)).toBeInTheDocument();
      sharedIndexStatus.mockRejectedValueOnce(new Error("boom"));
      codeIndexErrors.mockRejectedValueOnce(new Error("boom"));
      codeRecentCommits.mockRejectedValueOnce(new Error("boom"));
      await act(async () => {
        vi.advanceTimersByTime(60_000);
      });
      await waitFor(() => expect(screen.getByText(/couldn't refresh the index status/)).toBeInTheDocument());
      expect(screen.getByText(/generic profile/)).toBeInTheDocument();
      expect(screen.queryByText("Couldn't load index status")).toBeNull();
      // The sections read before keep their reads too.
      expect(screen.queryByText(/Couldn't load the index errors/)).toBeNull();
      expect(screen.queryByText(/Couldn't load the recent commits/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the first failure's line while a section keeps failing, so its alert is not said again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const down = (id: string) =>
        new ApiError({ status: 503, message: "the index store did not answer", requestId: id });
      codeCoverage.mockRejectedValueOnce(down("req-1")).mockRejectedValueOnce(down("req-2"));
      codeIndexErrors.mockResolvedValue({ rows: [], total: 0 });
      codeRecentCommits.mockResolvedValue({ rows: [] });
      render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
      const line = await screen.findByText(/^Couldn't load coverage: .*req-1/);
      const reads = codeCoverage.mock.calls.length;
      await act(async () => {
        vi.advanceTimersByTime(60_000);
      });
      await waitFor(() => expect(codeCoverage.mock.calls.length).toBeGreaterThan(reads));
      expect(line).toHaveTextContent(/req-1/);
      expect(screen.queryByText(/req-2/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("HealthTab: Refresh now by role", () => {
  const grant = (role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
    namespace: "code-app",
    role,
    status: "active",
    visibility: "private",
    description: "",
    joined_at: null,
    is_new: false,
  });
  function seed(role: MyNamespaceEntry["role"]) {
    resetNamespaceRoleCache();
    codeRefresh.mockClear();
    codeCoverage.mockResolvedValue(null);
    codeIndexErrors.mockResolvedValue({ rows: [], total: 0 });
    useStore.setState({
      whoami: { authenticated: true, subject: "p@example.com", org: "dev", roles: ["USER"], namespaces: ["code-app"] },
      myNamespaces: [grant(role)],
    });
  }
  afterEach(() => useStore.setState({ whoami: null, myNamespaces: null }));

  it("shows a reader Refresh now unavailable, named by the repository, and sends nothing", async () => {
    seed("viewer");
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
    const refresh = await screen.findByRole("button", { name: "Refresh now" });
    expect(refresh).toHaveAttribute("aria-disabled", "true");
    expect(refresh).toHaveAccessibleDescription(
      "Needs maintainer on acme/app @ main. A maintainer of it can grant that.",
    );
    refresh.click();
    expect(codeRefresh).not.toHaveBeenCalled();
  });

  it("keeps Refresh now unavailable for an admin claim's `*` token on a restricted repository", async () => {
    seed("admin");
    useStore.setState({
      whoami: { authenticated: true, subject: "a@example.com", org: "dev", roles: ["ADMIN"], namespaces: ["*"] },
      myNamespaces: [],
    });
    const restricted = { ...REPO, classification: "restricted" } as CodeRepoSummary;
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={restricted} />);
    const refresh = await screen.findByRole("button", { name: "Refresh now" });
    expect(refresh).toHaveAttribute("aria-disabled", "true");
    expect(refresh).toHaveAccessibleDescription(
      "To change its index, your token must name code-app, and you need a direct maintainer grant on acme/app @ main.",
    );
    refresh.click();
    expect(codeRefresh).not.toHaveBeenCalled();
  });

  it("reads the grant rows for an admin claim on a restricted repository, loading until they answer", async () => {
    seed("admin");
    invalidateShared();
    let answer: (rows: MyNamespaceEntry[]) => void = () => undefined;
    listMine.mockReturnValueOnce(new Promise((r) => (answer = r)));
    useStore.setState({
      whoami: { authenticated: true, subject: "a@example.com", org: "dev", roles: ["ADMIN"], namespaces: ["code-app"] },
      myNamespaces: null,
    });
    const restricted = { ...REPO, classification: "restricted" } as CodeRepoSummary;
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={restricted} />);
    const refresh = await screen.findByRole("button", { name: "Refresh now" });
    expect(refresh).toHaveAccessibleDescription("Your role on acme/app @ main is still loading.");
    await act(async () => answer([grant("admin")]));
    await waitFor(() => expect(refresh).not.toHaveAttribute("aria-disabled"));
    refresh.click();
    await waitFor(() => expect(codeRefresh).toHaveBeenCalled());
  });

  it("says a pending request to join waits for a maintainer", async () => {
    seed("viewer");
    useStore.setState({ myNamespaces: [{ ...grant("viewer"), status: "pending" }] });
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
    const refresh = await screen.findByRole("button", { name: "Refresh now" });
    expect(refresh).toHaveAccessibleDescription("Your request to join acme/app @ main is waiting for a maintainer.");
  });

  it("refreshes for a maintainer", async () => {
    seed("admin");
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} repo={REPO} />);
    const refresh = await screen.findByRole("button", { name: "Refresh now" });
    expect(refresh).not.toHaveAttribute("aria-disabled");
    refresh.click();
    await waitFor(() => expect(codeRefresh).toHaveBeenCalled());
  });
});
