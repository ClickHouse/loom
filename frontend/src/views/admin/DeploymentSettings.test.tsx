import { act, cleanup, fireEvent, render, screen, waitFor } from "../../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingChange, SettingRow } from "../../api";
import { useStore } from "../../lib/state";

const api = vi.hoisted(() => ({
  adminSettingsList: vi.fn(),
  adminSettingsHistory: vi.fn(),
  adminSettingSet: vi.fn(),
  adminSettingReset: vi.fn(),
}));
vi.mock("../../api", async (orig) => ({ ...(await orig<typeof import("../../api")>()), ...api }));
// The confirm dialog renders in the app shell, not here: every confirmation says yes.
vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn().mockResolvedValue(true) }));

import { DeploymentSettings, matching, parsed, undoIsReset } from "./DeploymentSettings";

const row = (over: Partial<SettingRow>): SettingRow => ({
  name: "memory_ask_recency_weight",
  env: "LOOM_MEMORY_ASK_RECENCY_WEIGHT",
  value: 0.35,
  default: 0.2,
  source: "override",
  live: true,
  description: "memory.ask only: re-rank the synthesis window",
  ...over,
});

const ROWS: SettingRow[] = [
  row({}),
  row({
    name: "ch_host",
    env: "LOOM_CH_HOST",
    value: "clickhouse",
    default: "localhost",
    source: "env",
    live: false,
    description: "ClickHouse host",
  }),
  row({ name: "memory_search_default_mode", value: "rrf", default: "rrf", source: "default", description: "mode" }),
];
const CHANGE: SettingChange = {
  ts: "2026-01-02 03:04:05.000",
  actor: "cli:ops",
  name: "memory_ask_recency_weight",
  value: 0.35,
  previous: 0.2,
  previous_source: "default",
  cleared: false,
};

beforeEach(() => {
  api.adminSettingsList.mockResolvedValue(ROWS);
  api.adminSettingsHistory.mockResolvedValue([CHANGE]);
  api.adminSettingSet.mockResolvedValue(row({ value: 0.4 }));
  useStore.setState({ whoami: { authenticated: true, roles: ["platform_admin"] } as never });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("matching", () => {
  it("keeps changed, live and matching settings", () => {
    expect(matching(ROWS, "", true, false).map((r) => r.name)).toEqual(["memory_ask_recency_weight", "ch_host"]);
    expect(matching(ROWS, "", false, true).map((r) => r.name)).toEqual([
      "memory_ask_recency_weight",
      "memory_search_default_mode",
    ]);
    expect(matching(ROWS, "synthesis", false, false).map((r) => r.name)).toEqual(["memory_ask_recency_weight"]);
  });
  it("reads an edit as JSON when it parses, else as text, and a string setting as text", () => {
    expect(parsed("0.4", { default: 0.2 })).toBe(0.4);
    expect(parsed("true", { default: false })).toBe(true);
    expect(parsed("rrf", { default: 3 })).toBe("rrf");
    expect(parsed("123", { default: "" })).toBe("123"); // a namespace named 123
  });
  it("calls an undo a reset unless the value before was an override, or there was none", () => {
    expect(undoIsReset(CHANGE)).toBe(true);
    expect(undoIsReset({ ...CHANGE, previous_source: "override" })).toBe(false);
    expect(undoIsReset({ ...CHANGE, previous_source: null })).toBe(false);
  });
  it("counts an override at the default as changed, as the server does", () => {
    const at = row({ value: 0.2, default: 0.2, source: "override" });
    expect(matching([at], "", true, false)).toEqual([at]);
    expect(matching([row({ value: 0.2, default: 0.2, source: "env" })], "", true, false)).toEqual([]);
  });
});

describe("DeploymentSettings", () => {
  it("lists the changed settings with their source and records", async () => {
    render(<DeploymentSettings />);
    expect(await screen.findAllByText("memory_ask_recency_weight")).toHaveLength(2); // the list and the record
    expect(screen.getByText("Environment")).toBeTruthy();
    expect(screen.queryByText("memory_search_default_mode")).toBeNull(); // at its default
    expect(screen.getByText("0.2 (undo: reset) → 0.35")).toBeTruthy();
    // A setting read at startup offers no change.
    expect(screen.queryByRole("button", { name: "Change ch_host" })).toBeNull();
  });

  it("changes a live setting, naming the value it replaces", async () => {
    render(<DeploymentSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Change memory_ask_recency_weight" }));
    fireEvent.change(screen.getByLabelText("New value for memory_ask_recency_weight"), { target: { value: "0.4" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save" }));
    });
    expect(api.adminSettingSet).toHaveBeenCalledWith("memory_ask_recency_weight", 0.4, 0.35);
    await waitFor(() => expect(api.adminSettingsList).toHaveBeenCalledTimes(2));
  });

  it("moves focus to the list when a reset takes the row off it", async () => {
    api.adminSettingReset.mockResolvedValue(row({ value: 0.2, source: "default" }));
    render(<DeploymentSettings />);
    const reset = await screen.findByRole("button", { name: "Reset memory_ask_recency_weight" });
    api.adminSettingsList.mockResolvedValue([row({ value: 0.2, source: "default" }), ROWS[1]]);
    reset.focus();
    fireEvent.click(reset);
    await waitFor(() => expect(api.adminSettingReset).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Change memory_ask_recency_weight" })).toBeNull());
    await waitFor(() => expect(document.activeElement?.id).toBe("settings-list"));
  });

  it("moves focus to the field it opens and back to Change when it closes", async () => {
    render(<DeploymentSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Change memory_ask_recency_weight" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByLabelText("New value for memory_ask_recency_weight")),
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change memory_ask_recency_weight" })),
    );
  });

  it("tells a caller without a platform admin claim, and reads nothing", () => {
    useStore.setState({ whoami: { authenticated: true, roles: ["org_admin"] } as never });
    render(<DeploymentSettings />);
    expect(screen.getByText("Platform admins only")).toBeTruthy();
    expect(api.adminSettingsList).not.toHaveBeenCalled();
  });
});
