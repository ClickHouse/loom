/**
 * The Health tab's coverage table and index-error list read beside the status row. A read
 * that fails ends its loading state with a stated error; it never leaves a loader up.
 */
import { cleanup, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
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
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    codeCoverage: (...a: unknown[]) => codeCoverage(...a),
    codeIndexErrors: (...a: unknown[]) => codeIndexErrors(...a),
    codeRecentCommits: (...a: unknown[]) => codeRecentCommits(...(a as [])),
    listCostRollup: async () => ({ rows: [] }),
  };
});
vi.mock("../lib/sharedReads", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/sharedReads")>();
  return { ...actual, sharedIndexStatus: async () => ST };
});

afterEach(() => cleanup());

describe("HealthTab: failed reads", () => {
  it("states that coverage, the index errors and the recent commits could not be read", async () => {
    codeCoverage.mockRejectedValue(new Error("boom"));
    codeIndexErrors.mockRejectedValue(new Error("boom"));
    codeRecentCommits.mockRejectedValueOnce(new Error("boom"));
    render(<HealthTab id={{ org: "dev", namespace: "code-app" }} />);
    expect(await screen.findByText("Couldn't load coverage.")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load the index errors.")).toBeInTheDocument();
    expect(screen.getByText("Couldn't load the recent commits.")).toBeInTheDocument();
    expect(screen.queryByText("No commits recorded yet.")).toBeNull();
  });
});
