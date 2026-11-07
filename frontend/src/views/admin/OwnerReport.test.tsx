import { act, cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { useStore } from "../../lib/state";
import { OwnerReportSection } from "./OwnerReport";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getOwnerReport: vi.fn(async () => ({
      user_id: "alice",
      live_rows: 42,
      reads: [
        {
          namespace: "team",
          agent: "support-bot",
          actor: "sa:bot",
          calls: 9,
          distinct_keys: 5,
          last_read: "2000-01-01 00:00:00",
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

describe("OwnerReportSection", () => {
  it("reads one user's memory and who read it", async () => {
    render(<OwnerReportSection />);
    await userEvent.type(screen.getByLabelText("User id"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    await waitFor(() =>
      expect(api.getOwnerReport).toHaveBeenCalledWith(
        expect.objectContaining({ namespace: "team" }),
        "alice",
        expect.anything(),
      ),
    );
    expect(await screen.findByText("alice owns 42 memories here.")).toBeInTheDocument();
    expect(screen.getByText("sa:bot")).toBeInTheDocument();
  });

  it("names the cut when the report lists as many readers as it asks for", async () => {
    const read = { namespace: "team", agent: "a", actor: "", calls: 1, distinct_keys: 1, last_read: "" };
    vi.mocked(api.getOwnerReport).mockResolvedValueOnce({
      user_id: "alice",
      live_rows: 3,
      reads: Array.from({ length: api.OWNER_READS }, (_, i) => ({ ...read, actor: `sa:${i}` })),
    });
    render(<OwnerReportSection />);
    await userEvent.type(screen.getByLabelText("User id"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByText(/The 50 most frequent readers:/)).toBeInTheDocument();
  });

  it("says why the report failed", async () => {
    vi.mocked(api.getOwnerReport).mockRejectedValueOnce(new Error("offline"));
    render(<OwnerReportSection />);
    await userEvent.type(screen.getByLabelText("User id"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("drops the report when the namespace changes", async () => {
    render(<OwnerReportSection />);
    await userEvent.type(screen.getByLabelText("User id"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(await screen.findByText("alice owns 42 memories here.")).toBeInTheDocument();
    act(() => useStore.setState({ identity: { org: "acme", namespace: "other", agent: "loom-ui" } }));
    expect(screen.queryByText(/alice owns/)).toBeNull();
  });
});
