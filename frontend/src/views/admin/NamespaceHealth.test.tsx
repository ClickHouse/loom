import { cleanup, render, screen } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../../lib/state";
import { NamespaceHealthPanel } from "./NamespaceHealth";

vi.mock("../../api/maintenance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/maintenance")>();
  return {
    ...actual,
    getMemoryHealth: vi.fn(async () => ({
      live: 120,
      recalled_ever: 80,
      used_ever: 40,
      silent: 30,
      silent_share: 0.25,
      silent_after_days: 30,
      by_type: [],
      staleness: { expiring_7d: 2, pinned: 1, days_since_reinforced_p50: 4 },
    })),
    getCalibration: vi.fn(async () => ({
      source: "namespace",
      embedder_model: "bge-small",
      noise_floor: 0.2,
      abstain_threshold: 0.31,
      conflict_threshold: 0.8,
      duplicate_threshold: 0.95,
    })),
    getExtractionBatches: vi.fn(async () => ({
      pending: 1,
      failed: 0,
      batches: [
        {
          queue_id: "q1",
          agent: "claude-code",
          status: "pending",
          attempts: 0,
          message_count: 12,
          written: 0,
          last_error: "",
          enqueued_at: null,
        },
      ],
    })),
  };
});

beforeEach(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
afterEach(() => cleanup());

describe("NamespaceHealthPanel", () => {
  it("shows the namespace's recall, its search gates and the waiting extractions", async () => {
    render(<NamespaceHealthPanel />);
    expect(await screen.findByText("25%")).toBeInTheDocument();
    expect(screen.getByText("bge-small")).toBeInTheDocument();
    expect(screen.getByText("0.310")).toBeInTheDocument();
    expect(screen.getByText("1 pending · 0 failed")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Waiting extractions" })).toBeInTheDocument();
  });
});
