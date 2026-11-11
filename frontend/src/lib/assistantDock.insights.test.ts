import { afterEach, describe, expect, it, vi } from "vitest";

// An old link to the Memories page's Insights tab (`/memories?section=insights`): the boot's
// address leaves the key out (it is no key of Memories), and once the shell has drawn the
// assistant panel opens on the audit card, nothing asked.

afterEach(() => {
  window.history.replaceState(null, "", "/");
  vi.resetModules();
});

describe("an old Insights link", () => {
  it("opens the panel on the audit card, once", async () => {
    window.history.replaceState(null, "", "/memories?section=insights");
    vi.resetModules();
    const dock = await import("./assistantDock");
    dock.bootDock();
    await vi.waitFor(() => expect(dock.useDock.getState().open).toBe(true));
    expect(dock.useDock.getState()).toMatchObject({ startersSeq: 1, startersAudit: true });
    // A second boot (a sign-in again) opens nothing more.
    dock.useDock.setState({ open: false, startersAudit: false });
    dock.bootDock();
    await new Promise((r) => setTimeout(r, 0));
    expect(dock.useDock.getState()).toMatchObject({ open: false, startersSeq: 1 });
  });

  it.each(["/memories", "/knowledge?section=insights"])("is not read from %s", async (address) => {
    window.history.replaceState(null, "", address);
    vi.resetModules();
    const dock = await import("./assistantDock");
    dock.bootDock();
    await new Promise((r) => setTimeout(r, 0));
    expect(dock.useDock.getState()).toMatchObject({ open: false });
    expect(dock.useDock.getState().startersSeq ?? 0).toBe(0);
  });
});
