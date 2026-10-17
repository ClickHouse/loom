import { act, renderHook } from "../test/rtl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../lib/state";
import type { TrackerSourceNamespace } from "../lib/trackerSources";
import { useRecordActions } from "./TrackerSources";

// Open on a tracker namespace switches the namespace and opens its Patterns board as a new
// history entry with a way back; the switch is said in a toast.

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast,
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  setSavedNamespace: vi.fn(),
}));

const ns: TrackerSourceNamespace = {
  namespace: "tracker-clickhouse",
  kind: "tracker",
  visibility: "public",
  repos: [],
  classification: "standard",
  join_policy: "open",
  registered: true,
  member_role: "viewer",
  pending_role: "",
};

describe("opening a tracker source", () => {
  beforeEach(() => {
    toast.mockReset();
    history.replaceState(null, "", "/repositories?repo=loom");
    useStore.setState({ identity: { org: "dev", namespace: "code-loom", agent: "loom-ui" }, view: "code" });
  });

  it("pushes the Patterns board with a way back to Repositories and toasts the switch", () => {
    const { result } = renderHook(() => useRecordActions("dev", async () => {}));
    const before = history.length;
    act(() => result.current.open(ns));
    expect(history.length).toBe(before + 1);
    expect(location.pathname + location.search).toBe("/graph?mode=patterns");
    expect(history.state).toEqual({ loomBack: "Repositories" });
    expect(useStore.getState()).toMatchObject({ view: "graph", identity: { namespace: "tracker-clickhouse" } });
    expect(toast).toHaveBeenCalledWith("Switched to tracker-clickhouse", "info");
  });

  it("Back to Repositories lands in the namespace it left, once", async () => {
    const first = renderHook(() => useRecordActions("dev", async () => {}));
    act(() => first.result.current.open(ns));
    first.unmount();
    await act(async () => {
      history.back();
      await new Promise((r) => window.addEventListener("popstate", r, { once: true }));
    });
    expect(location.pathname + location.search).toBe("/repositories?repo=loom");
    // The Repositories page mounts again on the entry it left.
    renderHook(() => useRecordActions("dev", async () => {}));
    expect(useStore.getState().identity?.namespace).toBe("code-loom");
    expect(toast).toHaveBeenLastCalledWith("Switched back to code-loom", "info");
    // Said once: the entry no longer asks for it, so a later switch made here survives a reload.
    expect((history.state as Record<string, unknown>).loomNamespaceLeft).toBeUndefined();
  });
});
