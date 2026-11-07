import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/maintenance";
import { confirmAction } from "../../components/ConfirmDialog";
import { useStore } from "../../lib/state";
import { RepairsPanel } from "./Repairs";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/maintenance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/maintenance")>();
  return {
    ...actual,
    sweepExpired: vi.fn(async () => ({ scanned: 9, tombstoned: 2, demoted: 0, anonymized: 0, locked_skipped: 1 })),
    reembedMemories: vi.fn(async () => ({ processed: 4, skipped_already_current: 0, target_model: "m" })),
    expireAdapterFacts: vi.fn(async () => ({ expired_triples: 5, expired_triple_memories: 2 })),
    rollbackNamespace: vi.fn(async () => ({ reinstate: 3, remove: 1, truncated: false })),
    snapshotNamespace: vi.fn(async () => ({ snapshot_id: "s1" })),
    restoreNamespace: vi.fn(async () => ({ restored: true, counts: { memories: 7 } })),
  };
});

const scope = { org: "acme", namespace: "team" };
beforeEach(() => useStore.setState({ identity: { ...scope, agent: "loom-ui" } }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RepairsPanel", () => {
  it("applies expiries after a dry run and a confirmation", async () => {
    render(<RepairsPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Apply expiries now" }));
    await waitFor(() => expect(api.sweepExpired).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.sweepExpired).mock.calls.map((c) => c[1])).toEqual([true, false]);
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Apply the expiry of 2 memories now?" }),
    );
  });

  it("removes one extractor's facts once it is named", async () => {
    render(<RepairsPanel />);
    const remove = screen.getByRole("button", { name: "Remove facts" });
    expect(remove).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Extractor"), "regex-v1");
    await userEvent.click(remove);
    await waitFor(() =>
      expect(api.expireAdapterFacts).toHaveBeenCalledWith(expect.objectContaining(scope), "regex-v1"),
    );
  });

  it("rolls back to a time only after counting what changes", async () => {
    render(<RepairsPanel />);
    await userEvent.type(screen.getByLabelText("As of"), "2000-01-02T03:04");
    await userEvent.click(screen.getByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(api.rollbackNamespace).toHaveBeenCalledTimes(2));
    const [first, second] = vi.mocked(api.rollbackNamespace).mock.calls;
    expect(first[2]).toBe(true);
    expect(second[2]).toBe(false);
    expect(first[1]).toBe(new Date("2000-01-02T03:04").toISOString());
  });

  it("restores a snapshot file into this namespace", async () => {
    render(<RepairsPanel />);
    const file = new File([JSON.stringify({ snapshot_id: "s1" })], "team.json", { type: "application/json" });
    await userEvent.upload(screen.getByLabelText("Snapshot file"), file);
    await waitFor(() => expect(api.restoreNamespace).toHaveBeenCalledWith("acme", "team", { snapshot_id: "s1" }));
  });
});
