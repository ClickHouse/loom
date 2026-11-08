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
      rows: [{ model: "claude-x", turns: 40, sessions: 3, cost_usd: 1.5, unpriced_turns: 0 }],
      total_cost_usd: 1.5,
      total_turns: 40,
      unpriced_turns: 2,
    });
    render(<AgentSpend />);
    expect(await screen.findByText("claude-x")).toBeInTheDocument();
    expect(screen.getByText("$1.5000 over the last 7 days, 2 turns unpriced")).toBeInTheDocument();
    expect(api.getAgentSpend).toHaveBeenCalledWith(
      expect.objectContaining({ namespace: "team" }),
      7,
      expect.anything(),
    );
  });
});
