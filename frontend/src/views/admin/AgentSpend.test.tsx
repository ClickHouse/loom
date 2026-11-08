import { cleanup, render, screen } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { useStore } from "../../lib/state";
import { AgentSpend } from "./AgentSpend";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getAgentSpend: vi.fn() };
});

beforeEach(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentSpend", () => {
  it("lists the coding agents' spend by model for the last week", async () => {
    vi.mocked(api.getAgentSpend).mockResolvedValue({
      rows: [
        { model: "claude-x", turns: 40, sessions: 3, cost_usd: 1.5, unpriced_turns: 0 },
        { model: "local-y", turns: 2, sessions: 1, cost_usd: 0, unpriced_turns: 2 },
      ],
      total_cost_usd: 1.5,
      total_turns: 42,
      unpriced_turns: 2,
    });
    render(<AgentSpend />);
    expect(await screen.findByText("claude-x")).toBeInTheDocument();
    expect(screen.getByText("$1.5000 over the last 7 days, 2 turns unpriced")).toBeInTheDocument();
    expect(screen.getByText("$1.5000")).toBeInTheDocument();
    // A model none of whose turns has a price reads as unpriced, not free.
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(api.getAgentSpend).toHaveBeenCalledWith(
      expect.objectContaining({ namespace: "team" }),
      7,
      expect.anything(),
    );
  });

  it("says a window with no priced turn has no cost, and counts one turn as a turn", async () => {
    vi.mocked(api.getAgentSpend).mockResolvedValueOnce({
      rows: [{ model: "local-y", turns: 2, sessions: 1, cost_usd: 0, unpriced_turns: 2 }],
      total_cost_usd: 0,
      total_turns: 2,
      unpriced_turns: 2,
    });
    render(<AgentSpend />);
    expect(await screen.findByText("2 turns over the last 7 days, none priced")).toBeInTheDocument();
    cleanup();
    vi.mocked(api.getAgentSpend).mockResolvedValueOnce({
      rows: [
        { model: "claude-x", turns: 4, sessions: 1, cost_usd: 0.25, unpriced_turns: 0 },
        { model: "local-y", turns: 1, sessions: 1, cost_usd: 0, unpriced_turns: 1 },
      ],
      total_cost_usd: 0.25,
      total_turns: 5,
      unpriced_turns: 1,
    });
    render(<AgentSpend />);
    expect(await screen.findByText("$0.2500 over the last 7 days, 1 turn unpriced")).toBeInTheDocument();
  });
});
