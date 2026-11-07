import { cleanup, render, screen, waitFor } from "../../test/rtl";
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
      expect(api.getOwnerReport).toHaveBeenCalledWith(expect.objectContaining({ namespace: "team" }), "alice"),
    );
    expect(await screen.findByText("alice owns 42 memories here.")).toBeInTheDocument();
    expect(screen.getByText("sa:bot")).toBeInTheDocument();
  });
});
