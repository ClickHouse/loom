import { cleanup, render, screen, waitFor, within } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api";
import { useStore } from "../../lib/state";
import { ExposurePanel } from "./Exposure";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getExposureSummary: vi.fn() };
});

beforeEach(() => useStore.setState({ identity: { org: "acme", namespace: "team", agent: "loom-ui" } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ExposurePanel", () => {
  it("lists each token's reads with its share of the namespace, and reads again for a new window", async () => {
    vi.mocked(api.getExposureSummary).mockResolvedValue({
      namespace_rows: 200,
      rows: [
        {
          actor: "sa:etl",
          agent: "loader",
          distinct_keys: 150,
          calls: 900,
          fraction: 0.75,
          first_hour: "2000-01-01 00:00:00",
          last_hour: "2000-01-01 03:00:00",
        },
      ],
    });
    render(<ExposurePanel />);
    const table = await screen.findByRole("table", { name: "Memory reads by token" });
    expect(await within(table).findByText("sa:etl")).toBeInTheDocument();
    expect(within(table).getByText("75%")).toBeInTheDocument();
    expect(api.getExposureSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({ namespace: "team" }),
      24,
      expect.anything(),
    );
    await userEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() =>
      expect(api.getExposureSummary).toHaveBeenLastCalledWith(expect.anything(), 168, expect.anything()),
    );
  });

  it("says when nothing read the namespace in the window", async () => {
    vi.mocked(api.getExposureSummary).mockResolvedValue({ namespace_rows: 0, rows: [] });
    render(<ExposurePanel />);
    expect(await screen.findByText("No reads in this window")).toBeInTheDocument();
  });
});
