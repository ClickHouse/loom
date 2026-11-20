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

// A bookmarked /assistant address: the boot lands on the landing page (no view is named),
// and once the shell has drawn the assistant opens over it, as the window from 1024 px.
describe("an /assistant address", () => {
  async function boot(address: string, width: number, state: Record<string, unknown> = {}) {
    window.history.replaceState(null, "", address);
    window.innerWidth = width;
    vi.resetModules();
    const dock = await import("./assistantDock");
    const { useStore } = await import("./state");
    useStore.setState({
      view: "memories",
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      deniedNamespace: null,
      ...state,
    });
    dock.bootDock();
    return dock;
  }
  afterEach(() => {
    window.innerWidth = 1024;
    localStorage.clear();
  });

  it.each(["/assistant", "/assistant/", "/?view=assistant"])("%s opens the window at 1280 px", async (address) => {
    const dock = await boot(address, 1280);
    await vi.waitFor(() => expect(dock.useDock.getState().open).toBe(true));
    expect(dock.useDock.getState()).toMatchObject({ expanded: true, expandedFromPush: false, focusField: true });
    // It appears from closed: the slide-in plays.
    expect(dock.useDock.getState().entering).toBe(true);
  });

  it("opens the docked panel below 1024 px", async () => {
    const dock = await boot("/assistant", 900);
    await vi.waitFor(() => expect(dock.useDock.getState().open).toBe(true));
    expect(dock.useDock.getState().expanded).toBe(false);
  });

  it("opens over a namespace that holds no memories, as a launcher click does", async () => {
    const dock = await boot("/assistant", 1280, { view: "code" });
    await vi.waitFor(() => expect(dock.useDock.getState().open).toBe(true));
  });

  it.each([
    ["Setup", { view: "setup" }],
    ["a namespace the link named and the account cannot read", { deniedNamespace: "other" }],
  ])("opens nothing on %s", async (_, state) => {
    const dock = await boot("/assistant", 1280, state);
    await new Promise((r) => setTimeout(r, 50));
    expect(dock.useDock.getState().open).toBe(false);
  });

  it("is not read from another page", async () => {
    const dock = await boot("/memories", 1280);
    await new Promise((r) => setTimeout(r, 50));
    expect(dock.useDock.getState().open).toBe(false);
  });
});

describe("with the chat off (__LOOM_ASSISTANT false)", () => {
  afterEach(() => {
    window.__LOOM_ASSISTANT = undefined;
    window.__LOOM_AUTH_DISABLED = undefined;
    window.innerWidth = 1024;
    localStorage.clear();
  });

  it("the reason is known from the first paint, whatever auth or the token says, and a stored open panel reopens", async () => {
    window.__LOOM_ASSISTANT = false;
    window.__LOOM_AUTH_DISABLED = true;
    localStorage.setItem("loom_assistant_panel", JSON.stringify({ open: true, width: 420 }));
    window.history.replaceState(null, "", "/memories");
    window.innerWidth = 1440;
    vi.resetModules();
    const dock = await import("./assistantDock");
    expect(dock.chatOff()).toBe(true);
    expect(dock.useDock.getState()).toMatchObject({
      reason: { kind: "not_configured" },
      reasonShort: "chat not set up",
    });
    dock.bootDock();
    await vi.waitFor(() => expect(dock.useDock.getState().open).toBe(true));
    // The runtime's claim reading leaves it.
    expect(dock.useDock.getState().reason).toEqual({ kind: "not_configured" });
  });

  it("a missing flag reads as on", async () => {
    vi.resetModules();
    const dock = await import("./assistantDock");
    expect(dock.chatOff()).toBe(false);
    expect(dock.useDock.getState().reason).toBeNull();
  });
});
