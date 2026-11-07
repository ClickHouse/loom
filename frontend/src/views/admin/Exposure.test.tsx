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
    const table = await screen.findByRole("table", { name: "Memory reads by reader" });
    expect(await within(table).findByText("sa:etl")).toBeInTheDocument();
    expect(within(table).getByText("75%")).toBeInTheDocument();
    expect(screen.getByText("1 reader of 200 memories")).toBeInTheDocument();
    expect(api.getExposureSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({ namespace: "team" }),
      24,
      200,
      expect.anything(),
    );
    await userEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() =>
      expect(api.getExposureSummary).toHaveBeenLastCalledWith(expect.anything(), 168, 200, expect.anything()),
    );
  });

  it("reads a fraction past the whole namespace as all, and a tiny one as under 1%", async () => {
    const row = (actor: string, fraction: number) => ({
      actor,
      agent: "a",
      distinct_keys: 1,
      calls: 1,
      fraction,
      first_hour: "2000-01-01 00:00:00",
      last_hour: "2000-01-01 00:00:00",
    });
    vi.mocked(api.getExposureSummary).mockResolvedValue({
      namespace_rows: 1000,
      rows: [row("sa:wide", 1.2), row("sa:thin", 0.001)],
    });
    render(<ExposurePanel />);
    const table = await screen.findByRole("table", { name: "Memory reads by reader" });
    expect(await within(table).findByText("all")).toBeInTheDocument();
    expect(within(table).getByText("<1%")).toBeInTheDocument();
    expect(screen.getByText("2 readers of 1,000 memories")).toBeInTheDocument();
  });

  it("names the cut when as many readers came back as it asks for", async () => {
    const row = (i: number) => ({
      actor: `sa:${i}`,
      agent: "a",
      distinct_keys: 1,
      calls: 1,
      fraction: 0.5,
      first_hour: "2000-01-01 00:00:00",
      last_hour: "2000-01-01 00:00:00",
    });
    vi.mocked(api.getExposureSummary).mockResolvedValue({
      namespace_rows: 2,
      rows: Array.from({ length: 200 }, (_, i) => row(i)),
    });
    render(<ExposurePanel />);
    expect(await screen.findByText("the 200 widest readers of 2 memories")).toBeInTheDocument();
  });

  it("reads any read of a namespace emptied since as all", async () => {
    vi.mocked(api.getExposureSummary).mockResolvedValue({
      namespace_rows: 0,
      rows: [
        {
          actor: "sa:leaked",
          agent: "a",
          distinct_keys: 150,
          calls: 900,
          fraction: 0,
          first_hour: "2000-01-01 00:00:00",
          last_hour: "2000-01-01 00:00:00",
        },
      ],
    });
    render(<ExposurePanel />);
    const table = await screen.findByRole("table", { name: "Memory reads by reader" });
    expect(await within(table).findByText("all")).toBeInTheDocument();
  });

  it("says why the read failed", async () => {
    vi.mocked(api.getExposureSummary).mockRejectedValue(new Error("offline"));
    render(<ExposurePanel />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("says when nothing read the namespace in the window", async () => {
    vi.mocked(api.getExposureSummary).mockResolvedValue({ namespace_rows: 0, rows: [] });
    render(<ExposurePanel />);
    expect(await screen.findByText("No reads in this window")).toBeInTheDocument();
  });
});
