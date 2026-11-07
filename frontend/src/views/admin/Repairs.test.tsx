import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/maintenance";
import { confirmAction } from "../../components/ConfirmDialog";
import { toast } from "../../components/Toast";
import { useStore } from "../../lib/state";
import { RepairsPanel } from "./Repairs";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/maintenance", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/maintenance")>();
  return {
    ...actual,
    sweepExpired: vi.fn(async () => ({
      scanned: 9,
      tombstoned: 2,
      demoted: 0,
      anonymized: 0,
      locked_skipped: 1,
      failures: [] as string[],
    })),
    reembedMemories: vi.fn(async () => ({ processed: 4, target_model: "m" })),
    expireAdapterFacts: vi.fn(async () => ({ expired_triples: 5, expired_triple_memories: 2 })),
    rollbackNamespace: vi.fn(async () => ({ reinstate: 3, remove: 1, truncated: false })),
    snapshotNamespace: vi.fn(async () => ({ snapshot_id: "s1" })),
    restoreNamespace: vi.fn(async () => ({ restored: true, counts: { memories: 7, erased_skipped: 0 } })),
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
      expect.objectContaining({ title: "Apply the expiry of 2 memories in team now?" }),
    );
  });

  it("applies nothing when no memory is due, or when the confirmation is declined", async () => {
    vi.mocked(api.sweepExpired).mockResolvedValueOnce({
      scanned: 9,
      tombstoned: 0,
      demoted: 0,
      anonymized: 0,
      locked_skipped: 0,
      failures: [],
    });
    render(<RepairsPanel />);
    const apply = screen.getByRole("button", { name: "Apply expiries now" });
    await userEvent.click(apply);
    await waitFor(() => expect(api.sweepExpired).toHaveBeenCalledTimes(1));
    expect(confirmAction).not.toHaveBeenCalled();
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    await userEvent.click(apply);
    await waitFor(() => expect(confirmAction).toHaveBeenCalled());
    // The second dry run, and no write after the declined confirmation.
    expect(vi.mocked(api.sweepExpired).mock.calls.map((c) => c[1])).toEqual([true, true]);
  });

  it("says an apply that did not all land failed", async () => {
    vi.mocked(api.sweepExpired)
      .mockResolvedValueOnce({ scanned: 9, tombstoned: 2, demoted: 0, anonymized: 0, locked_skipped: 0, failures: [] })
      .mockResolvedValueOnce({
        scanned: 9,
        tombstoned: 2,
        demoted: 0,
        anonymized: 0,
        locked_skipped: 0,
        failures: ["insert: down"],
      });
    render(<RepairsPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Apply expiries now" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Expired 2, but not all landed: insert: down", "error"));
  });

  it("says to run a full pass again only when it expired something", async () => {
    const pass = {
      scanned: api.SWEEP_LIMIT,
      tombstoned: 0,
      demoted: 0,
      anonymized: 0,
      locked_skipped: 0,
      failures: [],
    };
    vi.mocked(api.sweepExpired)
      .mockResolvedValueOnce({ ...pass, tombstoned: 3 })
      .mockResolvedValueOnce({ ...pass, tombstoned: 3 })
      .mockResolvedValueOnce({ ...pass, locked_skipped: api.SWEEP_LIMIT });
    render(<RepairsPanel />);
    const apply = screen.getByRole("button", { name: "Apply expiries now" });
    await userEvent.click(apply);
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Expired 3; 0 locked kept; run it again for the rest", "ok"),
    );
    // A preview that finds only locked rows says so and writes nothing.
    await userEvent.click(apply);
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(`${api.SWEEP_LIMIT} memories are past their expiry but locked.`, "info"),
    );
    expect(api.sweepExpired).toHaveBeenCalledTimes(3);
  });

  it("re-embeds, and says when none needs it or more remain", async () => {
    vi.mocked(api.reembedMemories)
      .mockResolvedValueOnce({ processed: 0, target_model: "m" })
      .mockResolvedValueOnce({ processed: api.REEMBED_LIMIT, target_model: "m" });
    render(<RepairsPanel />);
    const reembed = screen.getByRole("button", { name: "Re-embed stale vectors" });
    await userEvent.click(reembed);
    await waitFor(() => expect(toast).toHaveBeenCalledWith("No memory here needs re-embedding with m.", "info"));
    await userEvent.click(reembed);
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(`Re-embedded ${api.REEMBED_LIMIT} with m; run it again for the rest`, "ok"),
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
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({
        danger: true,
        message:
          "It holds an unknown number of memories, taken at an unknown time. Its memories replace their current versions here, deleted ones come back, and its grants are active again, including any revoked since.",
      }),
    );
  });

  it("counts only the memories a restore wrote, without the erased ones", async () => {
    vi.mocked(api.restoreNamespace).mockResolvedValueOnce({
      restored: true,
      counts: { memories: 7, erased_skipped: 2 },
    });
    render(<RepairsPanel />);
    const file = new File([JSON.stringify({ source_namespace: "team" })], "team.json", { type: "application/json" });
    await userEvent.upload(screen.getByLabelText("Snapshot file"), file);
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Restored 5 memories, 2 skipped as erased", "ok"));
  });

  it("says a snapshot of another namespace brings its members' grants here", async () => {
    render(<RepairsPanel />);
    const payload = { org: "acme", source_namespace: "team-a", created_at: "2000-01-01 00:00:00", memory_count: 3 };
    const file = new File([JSON.stringify(payload)], "team-a.json", { type: "application/json" });
    await userEvent.upload(screen.getByLabelText("Snapshot file"), file);
    await waitFor(() => expect(api.restoreNamespace).toHaveBeenCalled());
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({
        message:
          "It holds 3 memories, taken Jan 1, 2000, 00:00 UTC. It is a snapshot of team-a: its memories are written here, and its members get the grants they had there.",
      }),
    );
  });

  it("names the other org of a snapshot with the same namespace name", async () => {
    render(<RepairsPanel />);
    const payload = { org: "globex", source_namespace: "team", memory_count: 1 };
    const file = new File([JSON.stringify(payload)], "team.json", { type: "application/json" });
    await userEvent.upload(screen.getByLabelText("Snapshot file"), file);
    await waitFor(() => expect(api.restoreNamespace).toHaveBeenCalled());
    expect(confirmAction).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining("It is a snapshot of globex/team:") }),
    );
  });
});
