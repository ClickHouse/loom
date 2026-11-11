import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SearchHit } from "../api";
import { useStore } from "../lib/state";
import { useDock } from "../lib/assistantDock";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { AssistantPanel } from "../layout/AssistantPanel";
import { RecallLab } from "./RecallLab";

// Each Recall lab result row has its own "Ask": it names the result, its rank and the
// query in the assistant's next question, and opens no memory.

const searchMemory = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    searchMemory: (...args: unknown[]) => searchMemory(...args),
    listMemories: vi.fn(async () => ({ entries: [], total: 0, today: 0, truncated: false })),
    listSearchTraceLog: vi.fn(async () => ({ rows: [] })),
    isAuthDisabled: () => false,
  };
});

const hit = (key: string, rank_score: number): SearchHit => ({
  memory_key: key,
  memory_type: "semantic",
  content_excerpt: `excerpt of ${key}`,
  distance: 0.2,
  similarity: 0.8,
  rank_score,
  confidence: 1,
  score_components: { semantic: 0.6 },
});

async function score(text = "deploy") {
  fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: text } });
  return screen.findByRole("table", { name: "Recall scores" }, { timeout: 3000 });
}

describe("Ask about this on a Recall lab result", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNamespaceRoleCache();
    searchMemory.mockResolvedValue({ results: [hit("deploy_note", 0.9), hit("rollback_runbook", 0.4)] });
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "v", org: "dev", roles: ["VIEWER"], namespaces: ["ns"] },
      myNamespaces: [{ namespace: "ns", role: "viewer", status: "active", kind: "memory" } as never],
      identity: { org: "dev", namespace: "ns", agent: "loom-ui" },
      view: "recall",
      recallRunFilter: null,
      inspectedMemoryKey: null,
    });
    useDock.setState({
      open: false,
      mounted: false,
      draft: "",
      item: null,
      pageOff: false,
      viewport: 1440,
      reason: null,
      reasonShort: "",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ready: true, checks: { assistant: { ok: true } } }))),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    useStore.setState({ identity: null, inspectedMemoryKey: null, whoami: null, myNamespaces: null });
  });

  it("names the result and the query, opens the panel on its field and not the memory", async () => {
    render(
      <>
        <RecallLab />
        <AssistantPanel />
        <p id="loom-assistant-status" role="status" />
      </>,
    );
    const table = await score("deploy  steps");
    // A newer query typed while these rows show: the line names the one they answer.
    fireEvent.change(screen.getByLabelText("Recall query"), { target: { value: "rollback" } });
    const second = within(table).getAllByRole("row")[2];
    const ask = within(second).getByRole("button", { name: "Ask the assistant about this result" });
    expect(ask).toHaveTextContent("Ask");
    await userEvent.setup().click(ask);
    await waitFor(() => expect(screen.getByLabelText("Question")).toHaveFocus(), { timeout: 3000 });
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    expect(useDock.getState().item).toMatchObject({
      view: "recall",
      chip: "result rollback_runbook",
      line: 'result 2, memory "rollback_runbook", for the query "deploy steps"',
    });
    expect(screen.getByTestId("assistant-page-chip")).toHaveTextContent("Includes: Recall · result rollback_runbook");
  });

  it("quotes a key or query that holds quotes, as one JSON string each", async () => {
    searchMemory.mockResolvedValue({ results: [hit('say "hi"', 0.9)] });
    render(<RecallLab />);
    const table = await score('why "hi"');
    await userEvent.setup().click(within(table).getByRole("button", { name: "Ask the assistant about this result" }));
    await waitFor(() =>
      expect(useDock.getState().item?.line).toBe('result 1, memory "say \\"hi\\"", for the query "why \\"hi\\""'),
    );
  });

  it("offers no Ask, and no column for it, to a role the assistant's route does not admit", async () => {
    useStore.setState({ myNamespaces: [] });
    render(<RecallLab />);
    const table = await score();
    expect(within(table).queryByRole("button", { name: "Ask the assistant about this result" })).toBeNull();
    expect(within(table).getAllByRole("columnheader")).toHaveLength(7);
  });

  it("an unavailable Ask opens nothing either", async () => {
    useDock.setState({ reason: { kind: "starting" }, reasonShort: "setting up" });
    render(<RecallLab />);
    const table = await score();
    const ask = within(within(table).getAllByRole("row")[1]).getByRole("button", {
      name: "Ask the assistant about this result",
    });
    expect(ask).toHaveAccessibleDescription("The assistant can't answer now: setting up.");
    await userEvent.setup().click(ask);
    expect(useStore.getState().inspectedMemoryKey).toBeNull();
    expect(useDock.getState().open).toBe(false);
  });
});
