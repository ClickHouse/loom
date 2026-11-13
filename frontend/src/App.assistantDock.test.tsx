// The assistant panel in the shell: the header's Ask AI button, the modes the window's
// width gives it (push, overlay, phone sheet) and the window it expands to, what a modal
// panel does to the page (role, trap, inert), the keys (Esc, and that Ctrl+J, ⌘J and Ctrl+/
// open nothing), what navigations, Back and resizes do to it, and an /assistant address.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "./test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Each test boots the whole shell: more than the default 5 s on a loaded runner.
vi.setConfig({ testTimeout: 20_000 });

vi.mock("./views/registry", () => ({ preloadView: vi.fn(), VIEW_LOADERS: {} }));
vi.mock("./lib/navigation", () => ({ goToSso: vi.fn(), reloadPage: vi.fn() }));

import App from "./App";
import { useStore } from "./lib/state";
import { resetNamespaceRoleCache } from "./lib/namespaceRole";
import { invalidateShared } from "./lib/sharedReads";
import { useDock } from "./lib/assistantDock";
import { PANEL_DEFAULT } from "./lib/assistantLayout";
import { resetChat, useChat } from "./views/assistant/chatStore";

const ORG = "acme";
const NS = "team";
const GENERIC = {
  rows: [],
  entries: [],
  repos: [],
  results: {},
  missing: [],
  total: 0,
  today: 0,
  truncated: false,
  next_cursor: null,
  steps: [],
  status: "ok",
  problems: [],
  namespaces: [],
  agents: [],
  count: 0,
  totals: { namespaces: 0, agents: 0, cross_namespace_recalls: 0, writes: 0, recalls: 0 },
};

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** What `/v1/health/ready` says of the assistant: a check, or none (not configured). */
let assistantCheck: Record<string, unknown> | null = { ok: true, required: false };
let whoamiRoles = ["VIEWER"];
let role = "admin";
let paths: string[] = [];

async function until(cond: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!cond()) {
    if (Date.now() > deadline) throw new Error("condition not met in time");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
  }
}

function setWidth(width: number) {
  window.innerWidth = width;
}

async function boot(width: number, path = "/memories") {
  setWidth(width);
  window.history.replaceState(null, "", `${path}?namespace=${NS}`);
  render(<App />);
  await screen.findByRole("button", { name: "Ask AI" }, { timeout: 5000 });
  await until(() => useStore.getState().myNamespaces !== null);
}

const launcher = () => document.querySelector<HTMLElement>('[data-focus-key="assistant-launcher"]') as HTMLElement;
const panel = () => document.getElementById("assistant-panel") as HTMLElement;
const field = () => screen.getByLabelText("Question");

async function openByLauncher() {
  await userEvent.setup().click(launcher());
  await until(() => !!document.getElementById("assistant-question"));
  await until(() => document.activeElement?.id === "assistant-question");
}

// The media queries follow window.innerWidth, as a browser's do: a resize fires the
// resize event, then the queries' changes.
const mediaListeners = new Set<() => void>();
function stubMedia() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() {
      const min = /min-width: (\d+)px/.exec(query);
      const max = /max-width: (\d+)px/.exec(query);
      return min ? window.innerWidth >= Number(min[1]) : max ? window.innerWidth <= Number(max[1]) : false;
    },
    addEventListener: (_: string, f: () => void) => mediaListeners.add(f),
    removeEventListener: (_: string, f: () => void) => mediaListeners.delete(f),
  }));
}

async function resize(width: number) {
  await act(async () => {
    setWidth(width);
    window.dispatchEvent(new Event("resize"));
    for (const f of [...mediaListeners]) f();
  });
}

const inertOnPage = () => document.querySelector("[inert]");

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  invalidateShared();
  resetNamespaceRoleCache();
  resetChat(null);
  useChat.setState({ hint: null, readiness: null });
  useDock.setState({
    open: false,
    mounted: false,
    draft: "",
    working: false,
    unread: false,
    reason: null,
    focusField: false,
    prefWidth: PANEL_DEFAULT,
  });
  useStore.setState({
    authed: true,
    whoami: null,
    myNamespaces: null,
    identity: null,
    view: "memories",
    sessionExpired: false,
    sidebarCollapsed: false,
    helpOpen: false,
    inspectedMemoryKey: null,
  });
  Element.prototype.scrollTo = vi.fn();
  stubMedia();
  assistantCheck = { ok: true, required: false };
  whoamiRoles = ["VIEWER"];
  role = "admin";
  paths = [];
  localStorage.setItem("loom_token_v1", jwt({ sub: "alice", org: ORG, namespaces: [NS], roles: ["VIEWER"] }));
  localStorage.setItem("loom_kinds_v1", JSON.stringify({ [NS]: "memory" }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      const path = url.replace(/^https?:\/\/[^/]+/, "").split("?")[0];
      paths.push(path);
      if (path === "/v1/whoami")
        return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: whoamiRoles });
      if (path === "/v1/namespace.list_mine")
        return json({
          ...GENERIC,
          rows: [{ namespace: NS, role, status: "active", visibility: "private", kind: "memory" }],
        });
      if (path === "/v1/health/ready")
        return json({ ready: true, failing: [], checks: assistantCheck ? { assistant: assistantCheck } : {} });
      return json(GENERIC);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  setWidth(1024);
});

describe("the Ask AI button", () => {
  it("is one element at every width, in the header right after the search field", async () => {
    for (const width of [390, 700, 1280]) {
      await boot(width);
      expect(document.querySelectorAll('[data-focus-key="assistant-launcher"]')).toHaveLength(1);
      const header = document.querySelector("header") as HTMLElement;
      expect(header.contains(launcher())).toBe(true);
      // Next after the search in the tab order: no control between them.
      const search = document.getElementById("header-search") as HTMLElement;
      expect(search.compareDocumentPosition(launcher()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(search.nextElementSibling?.contains(launcher())).toBe(true);
      expect(launcher().closest("nav")).toBeNull();
      cleanup();
    }
  });

  it("is enabled from the first paint, opens the panel with the focus in the field, and closes it", async () => {
    await boot(1440);
    const button = launcher();
    expect(button).not.toBeDisabled();
    expect(button).not.toHaveAttribute("aria-disabled");
    expect(button).not.toHaveAttribute("aria-haspopup");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).not.toHaveAttribute("aria-controls");
    expect(button).toHaveAccessibleName("Ask AI");
    expect(button).toHaveTextContent("Ask AI");
    await openByLauncher();
    // Pressed while the panel is open.
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveAttribute("aria-controls", "assistant-panel");
    expect(field()).toHaveFocus();
    await userEvent.setup().click(button);
    await until(() => !useDock.getState().open);
    expect(button).toHaveAttribute("aria-expanded", "false");
  });

  it("below 1280 px, where it is its icon, its tooltip names it", async () => {
    await boot(1100);
    await userEvent.setup().hover(launcher());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/^Ask AI about this namespace$/);
    act(() => useDock.setState({ reason: { kind: "starting" }, reasonShort: "setting up" }));
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(/^Ask AI: setting up$/));
    act(() => useDock.setState({ reason: null, reasonShort: "" }));
  });

  it("its tooltip says what it does, or the reason it can't answer, and names no key", async () => {
    await boot(1440);
    await userEvent.setup().hover(launcher());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/^Ask AI about this namespace$/);
    act(() => useDock.setState({ reason: { kind: "starting" }, reasonShort: "setting up" }));
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(/^Ask AI: setting up$/));
    act(() => useDock.setState({ reason: { kind: "not_configured" }, reasonShort: "chat not set up" }));
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(/^Ask AI: chat not set up$/));
  });

  it("is the icon alone below 1280 px, named Ask AI, and on a phone opens the sheet", async () => {
    await boot(390);
    const button = launcher();
    expect(button).toHaveAccessibleName("Ask AI");
    expect(button).not.toHaveTextContent("Ask AI");
    expect(document.querySelector('nav[aria-label="Sections"]')?.textContent).not.toContain("Assistant");
    await openByLauncher();
    expect(panel()).toHaveAttribute("data-mode", "sheet");
  });

  it("keeps the focus across 1280 px and 768 px: the same element", async () => {
    await boot(1440);
    const button = launcher();
    act(() => button.focus());
    await resize(1100);
    expect(launcher()).toBe(button);
    expect(button).toHaveFocus();
    await resize(700);
    expect(launcher()).toBe(button);
    expect(button).toHaveFocus();
    await resize(1280);
    expect(button).toHaveFocus();
  });

  it("names its marks: answering, answer ready, unavailable", async () => {
    await boot(1440);
    act(() => useDock.setState({ working: true }));
    expect(launcher()).toHaveAccessibleName("Ask AI, answering");
    act(() => useDock.setState({ working: false, unread: true }));
    expect(launcher()).toHaveAccessibleName("Ask AI, answer ready");
    expect(document.querySelector(".assistant-launcher-dot")).not.toBeNull();
    act(() => useDock.setState({ reason: { kind: "starting" } }));
    expect(launcher()).toHaveAccessibleName("Ask AI, unavailable");
    // Never disabled: a click opens the panel to the reason.
    expect(launcher()).not.toHaveAttribute("aria-disabled");
    // A chat not set up is no broken mark: the starters still read.
    act(() => useDock.setState({ reason: { kind: "not_configured" }, unread: false }));
    expect(launcher()).toHaveAccessibleName("Ask AI");
    expect(launcher()).toHaveAttribute("data-launcher-state", "normal");
  });

  it("is inert under an open overlay, with the rest of the header", async () => {
    await boot(1100);
    await openByLauncher();
    expect(panel()).toHaveAttribute("aria-modal", "true");
    expect(launcher().closest("[inert]")).not.toBeNull();
  });

  it('"Go to Ask AI" opens the panel with the focus in the field, and stays a skip control', async () => {
    await boot(1440);
    const skip = screen.getByRole("button", { name: "Go to Ask AI" });
    expect(skip).toHaveAttribute("data-dock-inert");
    await userEvent.setup().click(skip);
    await until(() => document.activeElement?.id === "assistant-question");
    // Pressed again with the panel open, it keeps it open.
    act(() => skip.focus());
    await userEvent.setup().click(skip);
    await until(() => document.activeElement?.id === "assistant-question");
    expect(useDock.getState().open).toBe(true);
  });
});

describe("the expanded window (from 1024 px)", () => {
  const toggle = (name: string) => within(panel()).getByRole("button", { name });

  it("Expand makes the open panel a modal window over the page, with no address or history change", async () => {
    await boot(1440);
    await openByLauncher();
    const href = location.href;
    const length = history.length;
    const button = toggle("Expand");
    await userEvent.setup().click(button);
    expect(panel()).toHaveAttribute("data-mode", "expanded");
    expect(panel()).toHaveAttribute("role", "dialog");
    expect(panel()).toHaveAccessibleName("Ask AI");
    expect(screen.getByRole("heading", { level: 2, name: "Ask AI" })).toBeInTheDocument();
    expect(document.querySelector("main")?.closest("[inert]")).not.toBeNull();
    expect(launcher().closest("[inert]")).not.toBeNull();
    expect(location.href).toBe(href);
    expect(history.length).toBe(length);
    // One button whose label swaps: the focus stays on it.
    expect(button).toHaveAccessibleName("Collapse");
    expect(button).toHaveFocus();
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent("Ask AI fills the window"),
    );
    // From push, a spacer keeps <main>'s width under the window.
    expect(document.querySelector("[data-assistant-spacer]")).toHaveStyle({ width: "420px" });
    await userEvent.setup().click(button);
    expect(panel()).toHaveAttribute("data-mode", "push");
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent("Ask AI is beside the page"),
    );
  });

  it("Esc and a click on the scrim collapse it; Close closes it, and the next open is docked", async () => {
    await boot(1100);
    await openByLauncher();
    await userEvent.setup().click(toggle("Expand"));
    await userEvent.setup().keyboard("{Escape}");
    expect(panel()).toHaveAttribute("data-mode", "overlay");
    await waitFor(() =>
      expect(document.getElementById("loom-assistant-status")).toHaveTextContent("Ask AI is over the page"),
    );
    await userEvent.setup().click(toggle("Expand"));
    await userEvent.setup().click(document.querySelector(".assistant-scrim") as HTMLElement);
    expect(panel()).toHaveAttribute("data-mode", "overlay");
    await userEvent.setup().click(toggle("Expand"));
    await userEvent.setup().click(toggle("Close"));
    await until(() => !useDock.getState().open);
    expect(useDock.getState().expanded).toBe(false);
    await userEvent.setup().click(launcher());
    await until(() => useDock.getState().open);
    expect(panel()).toHaveAttribute("data-mode", "overlay");
  });

  it("is not offered below 1024 px, and a resize below it collapses it with the focus to the field", async () => {
    await boot(1440);
    await openByLauncher();
    await userEvent.setup().click(toggle("Expand"));
    await resize(900);
    expect(useDock.getState().expanded).toBe(false);
    expect(panel()).toHaveAttribute("data-mode", "overlay");
    expect(within(panel()).queryByRole("button", { name: /Expand|Collapse/ })).toBeNull();
    await until(() => document.activeElement?.id === "assistant-question");
    await resize(1440);
    expect(useDock.getState().expanded).toBe(false);
  });

  it("a navigation from it leaves the window, and beside the page the panel stays", async () => {
    await boot(1440);
    await openByLauncher();
    await userEvent.setup().click(toggle("Expand"));
    act(() => useStore.getState().setView("runs"));
    expect(useDock.getState()).toMatchObject({ open: true, expanded: false });
    expect(panel()).toHaveAttribute("data-mode", "push");
  });

  it("a session expiry leaves it: beside the page the panel stays, over the page it closes", async () => {
    await boot(1440);
    await openByLauncher();
    await userEvent.setup().click(toggle("Expand"));
    act(() => useStore.setState({ sessionExpired: true }));
    expect(useDock.getState()).toMatchObject({ open: true, expanded: false });
    expect(panel()).toHaveAttribute("data-mode", "push");
    // Expand is not offered while the session has expired: the window would cover its card.
    expect(within(panel()).queryByRole("button", { name: "Expand the assistant" })).toBeNull();
    act(() => useStore.setState({ sessionExpired: false }));
    await resize(1100);
    await userEvent.setup().click(toggle("Expand"));
    act(() => useStore.setState({ sessionExpired: true }));
    expect(useDock.getState()).toMatchObject({ open: false, expanded: false });
  });
});

// The boot's own cases are in lib/assistantDock.insights.test.ts (read as the module loads).
describe("an /assistant address", () => {
  it("an entry an earlier build pushed opens the window on Back, the address the landing page's", async () => {
    await boot(1440);
    window.history.pushState(null, "", "/assistant");
    act(() => useStore.getState().setView("runs"));
    await act(async () => {
      history.back();
      await new Promise((r) => setTimeout(r, 50));
    });
    await until(() => useDock.getState().open);
    expect(location.pathname).toBe("/memories");
    expect(useStore.getState().view).toBe("memories");
    expect(useStore.getState().identity?.namespace).toBe(NS);
    expect(useDock.getState().expanded).toBe(true);
  });
});

describe("push mode (from 1280 px, the page keeping 640 px beside the panel)", () => {
  it("is not modal: no dialog role, no trap, nothing inert, and Tab leaves the panel", async () => {
    await boot(1440);
    await openByLauncher();
    expect(panel()).toHaveAttribute("role", "region");
    expect(panel()).not.toHaveAttribute("aria-modal");
    expect(inertOnPage()).toBeNull();
    const ask = within(panel()).getByRole("button", { name: "Ask" });
    ask.focus();
    await userEvent.setup().tab();
    expect(panel().contains(document.activeElement)).toBe(false);
  });

  it("closes on Esc from inside, with the focus back on the launcher", async () => {
    await boot(1440);
    await openByLauncher();
    await userEvent.setup().keyboard("{Escape}");
    expect(panel()).toHaveAttribute("hidden");
    expect(launcher()).toHaveFocus();
  });

  it("leaves an Esc a layer above it already took", async () => {
    await boot(1440);
    await openByLauncher();
    const taken = (e: KeyboardEvent) => e.preventDefault();
    document.addEventListener("keydown", taken, { capture: true });
    fireEvent.keyDown(field(), { key: "Escape" });
    document.removeEventListener("keydown", taken, { capture: true });
    expect(panel()).not.toHaveAttribute("hidden");
  });

  it("has a splitter with its range, moved by the arrows, Home and End within the mode's widths", async () => {
    await boot(1440);
    await openByLauncher();
    const splitter = screen.getByRole("separator", { name: "Resize Ask AI" });
    expect(splitter).toHaveAttribute("aria-orientation", "vertical");
    expect(splitter).toHaveAttribute("aria-controls", "assistant-panel");
    expect(splitter).toHaveAttribute("aria-valuemin", "360");
    // 1440 - 244 (sidebar) - 640 (page) = 556: wider would leave the page under 640 px.
    expect(splitter).toHaveAttribute("aria-valuemax", "556");
    expect(splitter).toHaveAttribute("aria-valuenow", "420");
    splitter.focus();
    const user = userEvent.setup();
    await user.keyboard("{ArrowLeft}");
    expect(splitter).toHaveAttribute("aria-valuenow", "436");
    await user.keyboard("{End}");
    expect(splitter).toHaveAttribute("aria-valuenow", "556");
    await user.keyboard("{Home}");
    expect(splitter).toHaveAttribute("aria-valuenow", "360");
    expect(panel().style.width).toBe("360px");
  });

  it("stays open across a navigation, and a navigation from the panel keeps the focus there", async () => {
    await boot(1440);
    await openByLauncher();
    act(() => useStore.getState().setView("runs"));
    await until(() => !!document.querySelector("main h1"));
    await act(async () => new Promise((r) => setTimeout(r, 100)));
    expect(panel()).not.toHaveAttribute("hidden");
    expect(field()).toHaveFocus();
  });
});

describe("overlay mode (below the push threshold)", () => {
  it("is modal: a dialog, the page inert but not the panel, the toasts and live regions live, Tab wrapping", async () => {
    await boot(900);
    await openByLauncher();
    expect(panel()).toHaveAttribute("role", "dialog");
    expect(panel()).toHaveAttribute("aria-modal", "true");
    expect(document.querySelector("main")?.closest("[inert]")).not.toBeNull();
    expect(document.querySelector("header")?.closest("[inert]")).not.toBeNull();
    expect(document.querySelector('a[href="#main"]')).toHaveAttribute("inert");
    expect(field().closest("[inert]")).toBeNull();
    expect(document.getElementById("loom-assistant-status")?.closest("[inert]")).toBeNull();
    expect(document.getElementById("loom-nav-status")?.closest("[inert]")).toBeNull();
    // The last control wraps to the first.
    const ask = within(panel()).getByRole("button", { name: "Ask" });
    ask.focus();
    await userEvent.setup().tab();
    expect(panel().contains(document.activeElement)).toBe(true);
  });

  it("closes on a navigation, leaving nothing inert or modal, and the focus on the new page's h1", async () => {
    await boot(900);
    await openByLauncher();
    const original = HTMLElement.prototype.focus;
    let atFirstFocus: { inert: Element | null; modal: Element | null } | null = null;
    const spy = vi.spyOn(HTMLElement.prototype, "focus").mockImplementation(function (
      this: HTMLElement,
      opts?: FocusOptions,
    ) {
      if (this.tagName === "H1" && !atFirstFocus)
        atFirstFocus = { inert: inertOnPage(), modal: document.querySelector("[aria-modal]") };
      return original.call(this, opts);
    });
    act(() => useStore.getState().setView("runs"));
    // The modal attributes go in the same task as the navigation.
    expect(inertOnPage()).toBeNull();
    expect(document.querySelector("[aria-modal]")).toBeNull();
    expect(panel()).toHaveAttribute("hidden");
    await until(() => document.activeElement?.tagName === "H1");
    spy.mockRestore();
    // When the arrival first focused the h1, nothing was inert or modal any more.
    expect(atFirstFocus).toEqual({ inert: null, modal: null });
    // The conversation and the draft stay for the launcher.
    expect(useDock.getState().mounted).toBe(true);
  });

  it("closes on Back without touching history", async () => {
    await boot(900);
    act(() => useStore.getState().setView("runs"));
    await openByLauncher();
    const length = history.length;
    await act(async () => {
      history.back();
      await new Promise((r) => setTimeout(r, 50));
    });
    await until(() => panel().hasAttribute("hidden"));
    expect(history.length).toBe(length);
    expect(inertOnPage()).toBeNull();
  });
});

describe("keys", () => {
  it("opens nothing on Ctrl+J, ⌘J, Ctrl+/ or a bare j: Ask AI opens from its button and from search", async () => {
    await boot(1440);
    for (const key of [
      { key: "j", ctrlKey: true },
      { key: "j", metaKey: true },
      { key: "/", ctrlKey: true },
      { key: "j" },
    ])
      fireEvent.keyDown(document.body, key);
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(useDock.getState().open).toBe(false);
  });

  it("opens the panel to its starters with the chat off", async () => {
    window.__LOOM_ASSISTANT = false;
    try {
      await boot(1440);
      await userEvent.setup().click(launcher());
      await until(() => useDock.getState().open);
      expect(within(panel()).queryByRole("textbox", { name: "Question" })).toBeNull();
    } finally {
      window.__LOOM_ASSISTANT = undefined;
    }
  });
});

describe("mode changes", () => {
  it("keep the focus and the draft across a resize and a sidebar toggle", async () => {
    await boot(1440);
    await openByLauncher();
    await userEvent.setup().type(field(), "When do invoices go out?");
    await resize(900);
    expect(panel()).toHaveAttribute("aria-modal", "true");
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue("When do invoices go out?");
    await resize(1440);
    expect(panel()).not.toHaveAttribute("aria-modal");
    expect(field()).toHaveFocus();
    // Below 1280 px the panel is over the page with the sidebar open or collapsed.
    await resize(1279);
    expect(panel()).toHaveAttribute("aria-modal", "true");
    act(() => useStore.getState().toggleSidebar());
    expect(panel()).toHaveAttribute("aria-modal", "true");
    expect(field()).toHaveFocus();
    // At 1280 px it is beside the page with the sidebar collapsed (60), and open (244).
    await resize(1280);
    expect(panel()).not.toHaveAttribute("aria-modal");
    act(() => useStore.getState().toggleSidebar());
    expect(panel()).not.toHaveAttribute("aria-modal");
    expect(inertOnPage()).toBeNull();
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue("When do invoices go out?");
  });

  it("restores the panel open on reload only where it would push, and takes no focus then", async () => {
    localStorage.setItem("loom_assistant_panel", JSON.stringify({ open: true, width: 400 }));
    await boot(900);
    expect(useDock.getState().open).toBe(false);
    cleanup();
    useDock.setState({ open: false, mounted: false });
    await boot(1440);
    await until(() => !!document.getElementById("assistant-question"));
    expect(useDock.getState().open).toBe(true);
    expect(field()).not.toHaveFocus();
  });
});

describe("the phone sheet", () => {
  it("has its own history entry: Close goes back, and the focus returns to the launcher", async () => {
    await boot(375);
    // A navigation first: the current entry is the last, whatever Back an earlier test left.
    act(() => useStore.getState().setView("runs"));
    const length = history.length;
    await openByLauncher();
    expect(panel()).toHaveAttribute("data-mode", "sheet");
    expect(panel()).toHaveAttribute("aria-modal", "true");
    expect((history.state as Record<string, unknown>).loomSheet).toBe(true);
    expect(history.length).toBe(length + 1);
    await userEvent.setup().click(screen.getByRole("button", { name: "Close" }));
    await until(() => !(history.state as Record<string, unknown> | null)?.loomSheet);
    expect(panel()).toHaveAttribute("hidden");
    expect(launcher()).toHaveFocus();
  });

  it("closes on Back, and Forward onto its entry opens it again", async () => {
    await boot(375);
    await openByLauncher();
    await act(async () => {
      history.back();
      await new Promise((r) => setTimeout(r, 50));
    });
    await until(() => panel().hasAttribute("hidden"));
    await act(async () => {
      history.forward();
      await new Promise((r) => setTimeout(r, 50));
    });
    await until(() => !panel().hasAttribute("hidden"));
  });

  it("a navigation from it replaces its entry, so Back returns to the page under it", async () => {
    await boot(375);
    const length = history.length;
    await openByLauncher();
    act(() => useStore.getState().openDocument("doc://spec.pdf"));
    expect(panel()).toHaveAttribute("hidden");
    expect(inertOnPage()).toBeNull();
    expect(document.querySelector("[aria-modal]")).toBeNull();
    expect((history.state as Record<string, unknown>).loomSheet).toBeUndefined();
    expect(history.length).toBe(length + 1);
    expect(location.pathname).toBe("/library");
  });

  it("a resize to a docked width drops the sheet's stamp, and the open state stays", async () => {
    await boot(375);
    await openByLauncher();
    await resize(1440);
    expect((history.state as Record<string, unknown>).loomSheet).toBeNull();
    expect(useDock.getState().open).toBe(true);
    act(() => useStore.getState().setView("runs"));
    await act(async () => {
      history.back();
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(useDock.getState().open).toBe(true);
  });

  it("a reload on the sheet's entry shows the page, the entry unstamped", async () => {
    setWidth(375);
    window.history.replaceState({ loomSheet: true }, "", `/memories?namespace=${NS}`);
    render(<App />);
    await screen.findByRole("button", { name: "Ask AI" }, { timeout: 5000 });
    expect((history.state as Record<string, unknown>).loomSheet).toBeNull();
    expect(useDock.getState().open).toBe(false);
  });
});

describe("the assistant's live region", () => {
  it("is outside the panel from the first paint, and never inert", async () => {
    await boot(900);
    const region = document.getElementById("loom-assistant-status") as HTMLElement;
    expect(region).toHaveAttribute("role", "status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveAttribute("aria-atomic", "true");
    await openByLauncher();
    expect(panel().contains(region)).toBe(false);
    expect(region.closest("[inert]")).toBeNull();
    expect(region.closest('[aria-hidden="true"]')).toBeNull();
    // The inspector above the panel hides the rest of the page from assistive technology,
    // never the live regions.
    act(() => useStore.getState().inspect("billing/invoices", { agent: "*" }));
    await screen.findByRole("dialog", { name: /billing\/invoices|memory/i }, { timeout: 5000 });
    await until(() => !!document.querySelector('[aria-hidden="true"]'));
    expect(region.closest('[aria-hidden="true"]')).toBeNull();
    expect(panel()).toHaveAttribute("aria-modal", "true");
  });
});

describe("chat not set up", () => {
  it("links every reader, at overlay widths, to the docs on setting up the assistant service", async () => {
    assistantCheck = null;
    await boot(900);
    await userEvent.setup().click(launcher());
    const body = await screen.findByTestId("assistant-unavailable", {}, { timeout: 5000 });
    expect(body).toHaveTextContent("Chat isn't set up");
    expect(body).toHaveTextContent("An operator sets up the assistant service for this deployment.");
    expect(body).not.toHaveTextContent("Models & keys");
    await userEvent.setup().click(within(body).getByRole("button", { name: "How to set up the assistant service" }));
    await until(() => panel().hasAttribute("hidden"));
    expect(useStore.getState().view).toBe("help");
    expect(location.search).toContain("page=features");
    expect(location.hash).toBe("#the-deployed-assistant");
    expect(inertOnPage()).toBeNull();
  });
});

describe("readiness", () => {
  it("is read when the panel opens, not at boot", async () => {
    await boot(1440);
    expect(paths).not.toContain("/v1/health/ready");
    await openByLauncher();
    await until(() => paths.includes("/v1/health/ready"));
    expect(useChat.getState().readiness).toBeNull();
  });
});
