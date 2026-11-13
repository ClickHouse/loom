import { act, render, screen, waitFor } from "../test/rtl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/axe";
import { ApiError } from "../lib/errors";
import { KnowledgeGaps } from "./KnowledgeGaps";

// The Library's gaps tile: the grouped questions with their counts, what will appear on
// an empty list, and no tile where the op is refused or absent.

vi.mock("../api/knowledgeGaps", () => ({ listKnowledgeGaps: vi.fn() }));
import { listKnowledgeGaps } from "../api/knowledgeGaps";

const gap = {
  gap_id: "g1",
  head: "what is arr",
  samples: ["arr definition"],
  askers: 4,
  asks: 9,
  last_at: "2026-01-02T12:00:00",
};

describe("KnowledgeGaps", () => {
  beforeEach(() => {
    vi.mocked(listKnowledgeGaps).mockReset();
  });

  it("lists each gap with its askers, its asks and the questions merged into it", async () => {
    vi.mocked(listKnowledgeGaps).mockResolvedValue({
      gaps: [gap],
      built_at: "2026-01-02T13:00:00",
      enabled: true,
      capturing: true,
    });
    const { container } = render(<KnowledgeGaps org="o" namespace="ns" />);
    expect(await screen.findByText("what is arr")).toBeTruthy();
    expect(screen.getByText(/4 askers · 9 times/)).toBeTruthy();
    expect(screen.getByText("also: arr definition")).toBeTruthy();
    expect(vi.mocked(listKnowledgeGaps).mock.calls[0][0]).toEqual({ org: "o", namespace: "ns" });
    expect(await axeViolations(container)).toEqual([]);
  });

  it("says what will appear when there is no gap yet", async () => {
    vi.mocked(listKnowledgeGaps).mockResolvedValue({ gaps: [], built_at: null, enabled: true, capturing: true });
    render(<KnowledgeGaps org="o" namespace="ns" />);
    expect(await screen.findByText(/appear here, once a day/)).toBeTruthy();
  });

  it("says when recording searches is off for the namespace", async () => {
    vi.mocked(listKnowledgeGaps).mockResolvedValue({ gaps: [], built_at: null, enabled: true, capturing: false });
    render(<KnowledgeGaps org="o" namespace="ns" />);
    expect(await screen.findByText(/Recording searches is off for this namespace/)).toBeTruthy();
  });

  it("shows no tile when the deployment records no misses", async () => {
    vi.mocked(listKnowledgeGaps).mockResolvedValue({ gaps: [], built_at: null, enabled: false, capturing: null });
    const { container } = render(<KnowledgeGaps org="o" namespace="ns" />);
    await waitFor(() => expect(vi.mocked(listKnowledgeGaps).mock.settledResults[0]?.type).toBe("fulfilled"));
    await act(async () => {});
    expect(container.querySelector("section")).toBeNull();
  });

  it("says a failed read", async () => {
    vi.mocked(listKnowledgeGaps).mockRejectedValue(new ApiError({ status: 500, message: "boom" }));
    render(<KnowledgeGaps org="o" namespace="ns" />);
    expect(await screen.findByText(/Couldn't list the unanswered questions/)).toBeTruthy();
  });

  it("shows no tile when the role is refused", async () => {
    vi.mocked(listKnowledgeGaps).mockRejectedValue(new ApiError({ status: 403, code: "FORBIDDEN" }));
    const { container } = render(<KnowledgeGaps org="o" namespace="ns" />);
    await waitFor(() => expect(vi.mocked(listKnowledgeGaps).mock.settledResults[0]?.type).toBe("rejected"));
    await act(async () => {});
    expect(container.querySelector("section")).toBeNull();
  });
});
