import { cleanup, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry, RunRow, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { RUNS_FIELDS, Runs } from "./Runs";
import { getRun } from "../api";
import userEvent from "@testing-library/user-event";
import runsSource from "./Runs.tsx?raw";

// The Runs page asks run.list for a field list and the server blanks every field it does
// not name. A row that reads a field the list does not name (say `memories_used`) crashes
// `/runs` for every viewer with "Cannot read properties of undefined (reading
// 'length')". Two guards: the request names every field the row reads, and a row
// tolerates a projected list field anyway.

const listRuns = vi.fn<typeof import("../api").listRuns>();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    // The page reads run.list a page at a time; these tests answer with the rows alone.
    listRunsPage: async (...args: Parameters<typeof actual.listRunsPage>) => {
      const runs = await listRuns(...args);
      return { runs, total: runs.length, paged: true };
    },
    getRun: vi.fn(async () => ({ found: false, run: null })),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
  };
});

const row = (namespace: string, role: MyNamespaceEntry["role"]): MyNamespaceEntry => ({
  namespace,
  role,
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const owner: WhoAmI = {
  authenticated: true,
  subject: "o@example.com",
  org: "dev",
  roles: ["USER"],
  namespaces: ["ns"],
};

/** A run row as the projected list returns it: only the requested fields. */
function projected(fields: readonly string[], full: RunRow): RunRow {
  // Deliberate partial double: a projected row lacks list fields RunRow declares required.
  return Object.fromEntries(Object.entries(full).filter(([k]) => fields.includes(k) || k === "run_id")) as RunRow;
}

const FULL: RunRow = {
  run_id: "r-1",
  agent: "claude-code",
  user_id: "-",
  session_id: "s-1",
  goal: "fix the thing",
  user_request: "",
  expected_outcome: "",
  environment: "",
  model: "",
  consumer: "",
  status: "completed",
  started_at: "2026-09-20T07:00:00Z",
  completed_at: "2026-09-20T07:05:00Z",
  duration_ms: 300_000,
  final_response: "",
  confidence: 1,
  memories_used: ["a", "b"],
  memories_written: ["c"],
  metadata: {},
  budget_usd: 0,
  budget_seconds: 0,
  grade: "",
  grade_source: "",
  grade_reason: "",
  graded_at: "",
};

describe("Runs: the projected field list covers what the row reads", () => {
  it("RUNS_FIELDS names every field Runs.tsx reads off a row", () => {
    // Row parameters in this file are `r`; a `r.<field>` read is a field the list must carry.
    const read = new Set(Array.from(runsSource.matchAll(/\br\.([a-z_]+)/g), (m: RegExpMatchArray) => m[1]));
    for (const field of read) expect(RUNS_FIELDS, `row reads r.${field}`).toContain(field);
    // The opened row (`open`) is a list row too: the replay reads anything else off run.get.
    const opened = new Set(Array.from(runsSource.matchAll(/\bopen\??\.([a-z_]+)/g), (m: RegExpMatchArray) => m[1]));
    for (const field of opened) expect(RUNS_FIELDS, `the replay reads open.${field}`).toContain(field);
    expect(RUNS_FIELDS).toContain("memories_used");
    expect(RUNS_FIELDS).toContain("memories_written");
  });

  describe("rendering", () => {
    beforeEach(() => {
      vi.clearAllMocks();
      resetNamespaceRoleCache();
      useStore.setState({
        authed: true,
        whoami: owner,
        myNamespaces: [row("ns", "admin")],
        identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
        view: "runs",
      });
    });
    afterEach(() => {
      cleanup();
      useStore.setState({ whoami: null, myNamespaces: null, identity: null });
    });

    it("asks run.list for RUNS_FIELDS and renders a projected row's counts", async () => {
      listRuns.mockResolvedValue([projected(RUNS_FIELDS, FULL)]);
      render(<Runs />);
      await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(1));
      const [, opts] = listRuns.mock.calls[0];
      expect(opts?.fields).toEqual(RUNS_FIELDS);
      expect(await screen.findByText("fix the thing")).toBeInTheDocument();
    });

    it("the replay shows what only run.get carries: the request, the outcome, the response", async () => {
      listRuns.mockResolvedValue([projected(RUNS_FIELDS, FULL)]);
      vi.mocked(getRun).mockResolvedValue({
        found: true,
        run: { ...FULL, user_request: "make the tests pass", final_response: "done", confidence: 0.8 },
        recalled: 0,
        recalled_keys: [],
        memories_written: [],
        observations: 0,
        turns: 0,
        cost_usd: 0,
        unpriced_turns: 0,
        elapsed_seconds: 0,
        over_budget: [],
      } as never);
      render(<Runs />);
      await userEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
      expect(await screen.findByText("make the tests pass")).toBeInTheDocument();
      expect(screen.getByText("done")).toBeInTheDocument();
      expect(screen.getByText("confidence 0.80")).toBeInTheDocument();
    });

    it("opening another run never shows the run opened before's request", async () => {
      const second = { ...FULL, run_id: "r-2", goal: "second goal" };
      listRuns.mockResolvedValue([projected(RUNS_FIELDS, FULL), projected(RUNS_FIELDS, second)]);
      const answer = (run: RunRow) => ({
        found: true,
        run,
        recalled: 0,
        recalled_keys: [],
        memories_written: [],
        observations: 0,
        turns: 0,
        cost_usd: 0,
        unpriced_turns: 0,
        elapsed_seconds: 0,
        over_budget: [],
      });
      vi.mocked(getRun)
        .mockResolvedValueOnce(answer({ ...FULL, user_request: "the first request" }) as never)
        .mockReturnValueOnce(new Promise(() => {}));
      render(<Runs />);
      await userEvent.click(await screen.findByRole("button", { name: "fix the thing" }));
      expect(await screen.findByText("the first request")).toBeInTheDocument();
      await userEvent.keyboard("{Escape}");
      await userEvent.click(await screen.findByRole("button", { name: "second goal" }));
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      expect(screen.queryByText("the first request")).toBeNull();
    });

    it("a row missing a list field renders as zero, never a crash (the 93bf0598f defect)", async () => {
      const nineFields = RUNS_FIELDS.filter((f) => f !== "memories_used");
      listRuns.mockResolvedValue([projected(nineFields, FULL)]);
      render(<Runs />);
      await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("fix the thing")).toBeInTheDocument();
      expect(screen.queryByText(/Something went wrong/)).toBeNull();
    });
  });
});
