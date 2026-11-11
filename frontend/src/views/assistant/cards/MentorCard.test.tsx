import { cleanup, render, screen, waitFor, within } from "../../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MentorBriefResult, MyNamespaceEntry, WhoAmI } from "../../../api";
import { useStore } from "../../../lib/state";
import { resetNamespaceRoleCache } from "../../../lib/namespaceRole";
import { InsightsHarness, resetInsights } from "../../../test/insights";
import { humanizeHintCode, planeState, sortHints } from "./MentorBody";

// The mentor starter's card: `mentor.brief` for the current org/namespace, the namespace's
// most active writer by default over 7 days (the agent and window selects read again); the
// planes in use / never called, the hints critical → warning → info with their evidence and
// a link to the page that owns each hint's plane, and the adoption table. Nothing writes.

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
      plane: "runs",
    },
    {
      code: "code_index_unused",
      severity: "warning",
      text: "3 indexed repos and 0 code.* calls: code.search answers a concept.",
      ops: ["code.search", "code.symbol"],
      evidence: { repos: 3 },
      plane: "code index",
    },
  ],
  adoption: [{ code: "get_miss", shown_sessions: 4, followed_sessions: 3 }],
  agents: ["sre-agent", "claude-code"],
  agent_defaulted: true,
  skipped: ["row_shapes/oversized_values: TimeoutError"],
  healthy: false,
};

vi.mock("../../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => []),
    isAuthDisabled: () => false,
    mentorBrief: vi.fn(async () => BRIEF),
  };
});
import { mentorBrief } from "../../../api";

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

const STARTER = "What would Loom advise an agent writing here?";

describe("Assistant: the mentor card", () => {
  beforeEach(() => {
    resetInsights();
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
    resetInsights();
    useStore.setState({ whoami: null, myNamespaces: null, identity: null, inspectedMemoryKey: null });
  });

  it("reads when chosen with no agent over 7 days; the agent and window read again; planes, hints and adoption render", async () => {
    render(<InsightsHarness />);
    await screen.findByRole("button", { name: STARTER });
    expect(mentorBrief).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: STARTER }));
    await waitFor(() => expect(mentorBrief).toHaveBeenCalledTimes(1));
    const [scope, opts] = vi.mocked(mentorBrief).mock.calls[0];
    expect(scope).toEqual({ org: "dev", namespace: "team-alerts" });
    expect(opts).toMatchObject({ windowSeconds: 7 * 86_400 });
    expect(opts?.agent).toBeUndefined();

    const panel = await screen.findByTestId("assistant-card");
    expect(within(panel).getByRole("heading", { name: "Mentor" })).toBeInTheDocument();
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

    // The agent select lists the namespace's writers and reads again with no model call.
    const picker = within(panel).getByLabelText("Agent");
    expect(picker).toHaveTextContent("sre-agent");
    await userEvent.click(picker);
    const options = Array.from(document.querySelectorAll("[cui-select-item]"));
    expect(options.map((o) => o.getAttribute("data-value"))).toEqual(["sre-agent", "claude-code"]);
    await userEvent.click(await screen.findByText("claude-code"));
    await waitFor(() => expect(mentorBrief).toHaveBeenCalledTimes(2));
    expect(vi.mocked(mentorBrief).mock.calls[1][1]).toMatchObject({ agent: "claude-code" });

    const hints = await screen.findAllByTestId("mentor-hint");
    expect(hints.map((h) => h.getAttribute("data-severity"))).toEqual(["warning", "info"]);
    expect(hints.map((h) => h.getAttribute("data-code"))).toEqual(["code_index_unused", "runs_unused"]);
    expect(within(hints[0]).getByText("Code index unused")).toBeInTheDocument();
    expect(hints[0]).toHaveTextContent("evidence: repos=3");
    expect(hints[0]).toHaveTextContent("code.search · code.symbol");

    const adoption = screen.getByTestId("mentor-adoption");
    expect(adoption).toHaveTextContent("Get miss");
    expect(screen.getByTestId("mentor-skipped")).toHaveTextContent("row_shapes/oversized_values: TimeoutError");
    // Never a rewrite control.
    expect(within(panel).queryByRole("button", { name: /optimi[sz]e|apply|rewrite/i })).toBeNull();
  });

  it("links each hint to the page that owns its plane", async () => {
    const navigate = vi.fn();
    useStore.setState({ navigate });
    render(<InsightsHarness />);
    await userEvent.click(await screen.findByRole("button", { name: STARTER }));
    const hints = await screen.findAllByTestId("mentor-hint");
    await userEvent.click(within(hints[0]).getByRole("button", { name: "Open in Repositories" }));
    expect(navigate).toHaveBeenCalledWith("code", {});
    await userEvent.click(within(hints[1]).getByRole("button", { name: "Open in Runs" }));
    expect(navigate).toHaveBeenLastCalledWith("runs", {});
  });

  it("a hint from a server that names no plane, or one with no page, has no link", async () => {
    vi.mocked(mentorBrief).mockResolvedValueOnce({
      ...BRIEF,
      hints: BRIEF.hints.map(({ plane: _plane, ...h }, i) => (i === 0 ? h : { ...h, plane: "scratchpad" })),
    });
    render(<InsightsHarness />);
    await userEvent.click(await screen.findByRole("button", { name: STARTER }));
    const hints = await screen.findAllByTestId("mentor-hint");
    for (const h of hints) expect(within(h).queryByRole("button", { name: /^Open in/ })).toBeNull();
  });

  it("a stranger to the namespace is offered no read", async () => {
    useStore.setState({ myNamespaces: [], whoami: { ...owner, namespaces: [] } });
    render(<InsightsHarness />);
    const starter = await screen.findByRole("button", { name: STARTER });
    expect(starter).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(starter);
    expect(mentorBrief).not.toHaveBeenCalled();
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
