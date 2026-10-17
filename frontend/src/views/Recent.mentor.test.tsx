import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MemoryListEntry, MentorBriefResult, MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { setFoldOpen } from "../lib/folds";
import { Recent } from "./Recent";
import { humanizeHintCode, planeState, sortHints } from "../components/MentorPanel";

// The Memories view carries a "Mentor" panel. One click runs `mentor.brief` for the
// current org/namespace and an agent name; the panel renders the planes in use / never
// called, the hints critical → warning → info with their evidence, and the adoption
// table. Nothing runs on mount; nothing here writes.

const entry = (memory_key: string): MemoryListEntry => ({
  memory_key,
  memory_type: "semantic",
  confidence: 1,
  version: 1,
  created_at: "2026-09-18T09:00:00Z",
  expires_at: null,
  pinned: false,
  tags: [],
  agent: "claude-desktop",
  value: `value of ${memory_key}`,
});

const BRIEF: MentorBriefResult = {
  org: "dev",
  namespace: "team-alerts",
  agent: "sre-agent",
  window_seconds: 7 * 86_400,
  computed_at: "2026-09-19T12:00:00Z",
  sessions: 12,
  usage: { "memory.set": 1159, "memory.search": 5 },
  planes: [
    { plane: "claims", calls: 1164, available: true, when: "one plain-text claim per readable key", mentored: true },
    { plane: "code index", calls: 0, available: true, when: "identifier questions", mentored: true },
    { plane: "test signal", calls: 0, available: null, when: "test failures and reports", mentored: true },
    { plane: "documents", calls: 0, available: false, when: "anything over ~2 KB", mentored: true },
    { plane: "erasure", calls: 0, available: true, when: "a person's right to be forgotten", mentored: false },
  ],
  hints: [
    // Deliberately out of order: the panel sorts by severity.
    {
      code: "runs_unused",
      severity: "info",
      text: "12 sessions and no run ledger: run.begin before the work.",
      ops: ["run.begin", "run.complete"],
      evidence: { sessions: 12 },
    },
    {
      code: "code_index_unused",
      severity: "warning",
      text: "3 indexed repos and 0 code.* calls: code.search answers a concept.",
      ops: ["code.search", "code.symbol"],
      evidence: { repos: 3 },
    },
  ],
  adoption: [{ code: "get_miss", shown_sessions: 4, followed_sessions: 3 }],
  agents: ["sre-agent", "claude-code"],
  agent_defaulted: true,
  skipped: ["row_shapes/oversized_values: TimeoutError"],
  healthy: false,
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMemories: vi.fn(async () => ({ entries: [entry("coffee_pref")], total: 1, today: 1 })),
    bulkGetMemories: vi.fn(async () => ({ results: {}, missing: [] })),
    listConflicts: vi.fn(async () => ({ rows: [] })),
    conflictKeys: vi.fn(async (): Promise<string[]> => []),
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    auditNamespace: vi.fn(async () => {
      throw new Error("not in this test");
    }),
    mentorBrief: vi.fn(async () => BRIEF),
  };
});
import { listMemories, mentorBrief } from "../api";

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
  namespaces: ["team-alerts"],
};

// The panel lives on the Memories page's Insights tab (`?section=insights`), not above
// the list.
async function openMemories() {
  window.history.replaceState(null, "", "/memories?section=insights");
  render(<Recent />);
  await screen.findByTestId("mentor-panel");
  expect(screen.getByRole("tab", { name: "Insights" })).toHaveAttribute("aria-selected", "true");
  // Insights reads no rows.
  expect(listMemories).not.toHaveBeenCalled();
}

describe("Memories: mentor panel", () => {
  beforeEach(() => {
    // The panel starts folded; these tests are about what it shows once opened.
    setFoldOpen("mentor", true);
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({
      authed: true,
      whoami: owner,
      myNamespaces: [row("team-alerts", "admin")],
      identity: { org: "dev", namespace: "team-alerts", agent: "loom-ui" },
      view: "memories",
      inspectedMemoryKey: null,
    });
  });
  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("runs on a click with the agent and window; planes, hints and adoption render", async () => {
    await openMemories();
    const panel = screen.getByTestId("mentor-panel");
    expect(within(panel).getByRole("heading", { name: "Mentor" })).toBeInTheDocument();
    expect(mentorBrief).not.toHaveBeenCalled();

    // First ask: no agent typed — the server picks the most active writer.
    await userEvent.click(within(panel).getByLabelText("Mentor window"));
    await userEvent.click(await screen.findByText("30 days"));
    await userEvent.click(within(panel).getByRole("button", { name: /Ask the mentor/ }));

    await waitFor(() => expect(mentorBrief).toHaveBeenCalledTimes(1));
    const [scope, opts] = vi.mocked(mentorBrief).mock.calls[0];
    expect(scope).toEqual({ org: "dev", namespace: "team-alerts" });
    expect(opts).toMatchObject({ windowSeconds: 30 * 86_400 });
    expect(opts?.agent).toBeUndefined();

    const summary = await screen.findByTestId("mentor-summary");
    expect(summary).toHaveTextContent("Has planes it never uses");
    expect(summary).toHaveTextContent("12 sessions over 7d");
    expect(summary).toHaveTextContent("in use claims");
    expect(summary).toHaveTextContent("never called code index");
    expect(summary).toHaveTextContent("the most active writer here");
    // A plane the namespace lacks is not listed; an unknown one is not "never called";
    // an un-mentored plane (erasure) is never "never called".
    expect(summary).not.toHaveTextContent("documents");
    expect(summary).not.toHaveTextContent("test signal");
    expect(summary).not.toHaveTextContent("erasure");
    // After the first brief the agent control is a picker over the namespace's writers.
    const picker = within(panel).getByLabelText("Agent");
    expect(picker).toHaveTextContent("sre-agent");
    await userEvent.click(picker);
    const options = Array.from(document.querySelectorAll("[cui-select-item]"));
    expect(options.map((o) => o.getAttribute("data-value"))).toEqual(["sre-agent", "claude-code"]);
    await userEvent.click(await screen.findByText("claude-code"));
    await userEvent.click(within(panel).getByRole("button", { name: /Ask again/ }));
    await waitFor(() => expect(mentorBrief).toHaveBeenCalledTimes(2));
    expect(vi.mocked(mentorBrief).mock.calls[1][1]).toMatchObject({ agent: "claude-code" });

    const hints = screen.getAllByTestId("mentor-hint");
    expect(hints.map((h) => h.getAttribute("data-severity"))).toEqual(["warning", "info"]);
    expect(hints.map((h) => h.getAttribute("data-code"))).toEqual(["code_index_unused", "runs_unused"]);
    expect(within(hints[0]).getByText("Code index unused")).toBeInTheDocument();
    expect(hints[0]).toHaveTextContent("evidence: repos=3");
    expect(hints[0]).toHaveTextContent("code.search · code.symbol");

    const adoption = screen.getByTestId("mentor-adoption");
    expect(adoption).toHaveTextContent("Get miss");
    expect(adoption).toHaveTextContent("4");
    expect(adoption).toHaveTextContent("3");
    expect(screen.getByTestId("mentor-skipped")).toHaveTextContent("row_shapes/oversized_values: TimeoutError");
    // Never a rewrite control.
    expect(within(panel).queryByRole("button", { name: /optimi[sz]e|apply|rewrite/i })).toBeNull();
  });

  it("a viewer of the namespace sees the panel; a stranger does not", async () => {
    useStore.setState({ myNamespaces: [row("team-alerts", "viewer")] });
    await openMemories();
    expect(screen.getByTestId("mentor-panel")).toBeInTheDocument();
    cleanup();
    resetNamespaceRoleCache();
    useStore.setState({ myNamespaces: [], whoami: { ...owner, namespaces: [] } });
    render(<Recent />);
    await screen.findByRole("tab", { name: "Insights" });
    expect(screen.queryByTestId("mentor-panel")).toBeNull();
  });

  it("helpers: humanised codes, severity order, plane states", () => {
    expect(humanizeHintCode("code_index_unused")).toBe("Code index unused");
    expect(humanizeHintCode("some_new_rule")).toBe("Some new rule");
    expect(sortHints(BRIEF.hints).map((h) => h.code)).toEqual(["code_index_unused", "runs_unused"]);
    expect(planeState({ plane: "x", calls: 3, available: false, when: "", mentored: true })).toBe("in use");
    expect(planeState({ plane: "x", calls: 0, available: true, when: "", mentored: true })).toBe("available");
    expect(planeState({ plane: "x", calls: 0, available: null, when: "", mentored: true })).toBe("unknown");
    expect(planeState({ plane: "x", calls: 0, available: false, when: "", mentored: true })).toBeNull();
    expect(planeState({ plane: "erasure", calls: 0, available: true, when: "", mentored: false })).toBeNull();
    expect(planeState({ plane: "erasure", calls: 2, available: true, when: "", mentored: false })).toBe("in use");
  });
});
