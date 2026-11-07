import { cleanup, render, screen } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import * as api from "../../api/maintenance";
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
      silent_share: 0.25,
      silent_after_days: 30,
      staleness: { expiring_7d: 2 },
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
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("NamespaceHealthPanel", () => {
  it("shows the namespace's recall, its search gates and its recent extractions", async () => {
    render(<NamespaceHealthPanel />);
    expect(await screen.findByText("25%")).toBeInTheDocument();
    expect(screen.getByText("bge-small")).toBeInTheDocument();
    expect(screen.getByText("0.310")).toBeInTheDocument();
    expect(screen.getByText("From this namespace's calibration.")).toBeInTheDocument();
    expect(screen.getByText("1 batch: 1 waiting, 0 failed")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Recent extractions" })).toBeInTheDocument();
  });

  it("keeps the other sections when one read fails, and Retry reads that one again", async () => {
    vi.mocked(api.getCalibration).mockRejectedValueOnce(new Error("down"));
    render(<NamespaceHealthPanel />);
    expect(await screen.findByText("25%")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText("bge-small")).toBeInTheDocument();
    expect(api.getCalibration).toHaveBeenCalledTimes(2);
    expect(api.getMemoryHealth).toHaveBeenCalledTimes(1);
  });

  it("says when no transcript was queued", async () => {
    vi.mocked(api.getExtractionBatches).mockResolvedValueOnce({ pending: 0, failed: 0, batches: [] });
    render(<NamespaceHealthPanel />);
    expect(await screen.findByText("No transcript was queued for extraction.")).toBeInTheDocument();
  });
});
