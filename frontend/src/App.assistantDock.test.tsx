// The assistant panel in the shell: the launcher, the three modes the window's width gives
// it (push, overlay, phone sheet), what a modal panel does to the page (role, trap, inert),
// the keys (Esc, Ctrl+/), and what navigations, Back and resizes do to it.
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
  await screen.findByRole("button", { name: "Assistant" }, { timeout: 5000 });
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

async function resize(width: number) {
  await act(async () => {
    setWidth(width);
    window.dispatchEvent(new Event("resize"));
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
  assistantCheck = { ok: true, required: false };
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
        return json({ authenticated: true, subject: "alice", org: ORG, namespaces: [NS], roles: ["VIEWER"] });
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

describe("the launcher", () => {
  it("is in the header from the first paint, enabled, and opens the panel with the focus in the field", async () => {
    await boot(1440);
    const header = document.querySelector("header") as HTMLElement;
    const button = within(header).getByRole("button", { name: "Assistant" });
    expect(button).not.toBeDisabled();
    expect(button).not.toHaveAttribute("aria-disabled");
    expect(button).toHaveAttribute("aria-expanded", "false");
    // Left of the account menu.
    const account = within(header).getByRole("button", { name: /^Account:/ });
    expect(button.compareDocumentPosition(account) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await openByLauncher();
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(field()).toHaveFocus();
  });

  it("shows its word from xl, one element with the same names, its tooltip only the shortcut", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query === "(min-width: 1280px)" || query === "(min-width: 1024px)",
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    await boot(1440);
    expect(document.querySelectorAll('[data-focus-key="assistant-launcher"]')).toHaveLength(1);
    expect(launcher()).toHaveTextContent("Assistant");
    expect(launcher()).toHaveAccessibleName("Assistant");
    act(() => useDock.setState({ working: true }));
    expect(launcher()).toHaveAccessibleName("Assistant, answering");
    act(() => useDock.setState({ working: false }));
    await userEvent.setup().hover(launcher());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(/^Ctrl \/$/);
  });

  it("keeps the focus when the window crosses 1280 px: one launcher, its tooltip changing", async () => {
    let wide = false;
    const listeners = new Set<() => void>();
    vi.stubGlobal("matchMedia", (query: string) => ({
      get matches() {
        return query === "(min-width: 1280px)" ? wide : query === "(min-width: 1024px)";
      },
      addEventListener: (_: string, f: () => void) => listeners.add(f),
      removeEventListener: (_: string, f: () => void) => listeners.delete(f),
    }));
    await boot(1440);
    const button = launcher();
    act(() => button.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Assistant (Ctrl /)");
    wide = true;
    act(() => {
      for (const f of listeners) f();
    });
    expect(launcher()).toBe(button);
    expect(button).toHaveFocus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(/^Ctrl \/$/));
  });

  it("names its marks: answering, answer ready, unavailable", async () => {
    await boot(1440);
    act(() => useDock.setState({ working: true }));
    expect(launcher()).toHaveAccessibleName("Assistant, answering");
    act(() => useDock.setState({ working: false, unread: true }));
    expect(launcher()).toHaveAccessibleName("Assistant, answer ready");
    act(() => useDock.setState({ reason: { kind: "starting" } }));
    expect(launcher()).toHaveAccessibleName("Assistant, unavailable");
    // Never disabled: a click opens the panel to the reason.
    expect(launcher()).not.toHaveAttribute("aria-disabled");
  });
});

describe("push mode (the page keeps 640 px beside the panel)", () => {
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
    const splitter = screen.getByRole("separator", { name: "Resize the assistant panel" });
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

  it("leaves Ctrl+/ to another dialog that holds the focus", async () => {
    await boot(900);
    act(() => useStore.getState().openHelp(true));
    const dialog = await screen.findByRole("dialog");
    await until(() => dialog.contains(document.activeElement));
    fireEvent.keyDown(document.activeElement as Element, { key: "/", ctrlKey: true });
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(useDock.getState().open).toBe(false);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

describe("the shortcut", () => {
  it("Ctrl+/ and ⌘/ open and close the panel; a close from outside leaves the focus where it was", async () => {
    await boot(1440);
    fireEvent.keyDown(document.body, { key: "/", ctrlKey: true });
    await until(() => document.activeElement?.id === "assistant-question");
    fireEvent.keyDown(field(), { key: "/", metaKey: true });
    await until(() => !useDock.getState().open);
    expect(launcher()).toHaveFocus();
    fireEvent.keyDown(document.body, { key: "/", metaKey: true });
    await until(() => useDock.getState().open);
    const other = screen.getByRole("button", { name: "Keyboard shortcuts" });
    other.focus();
    fireEvent.keyDown(other, { key: "/", ctrlKey: true });
    await until(() => !useDock.getState().open);
    expect(other).toHaveFocus();
  });

  it("a bare / is not the panel's", async () => {
    await boot(1440);
    fireEvent.keyDown(document.body, { key: "/" });
    await act(async () => new Promise((r) => setTimeout(r, 50)));
    expect(useDock.getState().open).toBe(false);
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
    // 1100 - 244 - 640 leaves less than 360: overlay; with the sidebar collapsed (60), push.
    await resize(1100);
    expect(panel()).toHaveAttribute("aria-modal", "true");
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
    await userEvent.setup().click(screen.getByRole("button", { name: "Close the assistant" }));
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
    await screen.findByRole("button", { name: "Assistant" }, { timeout: 5000 });
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

describe("the Health link", () => {
  it("at overlay widths closes the panel and lands the focus on Health's assistant row", async () => {
    assistantCheck = { ok: false, reason: "failing", required: false };
    await boot(900);
    await userEvent.setup().click(launcher());
    const link = await screen.findByRole("button", { name: "see Health" }, { timeout: 5000 });
    expect(screen.getByTestId("assistant-unavailable")).toHaveTextContent(
      "An admin of this deployment fixes the assistant's setup. Loom retries the setup on its own; this updates when it works.",
    );
    await userEvent.setup().click(link);
    expect(panel()).toHaveAttribute("hidden");
    await until(() => document.activeElement?.getAttribute("data-focus-key") === "health-assistant");
    expect(inertOnPage()).toBeNull();
  });

  it("falls back to the h1 at once when the row does not show, without waiting out the timeout", async () => {
    assistantCheck = { ok: false, reason: "failing", required: false };
    await boot(900);
    await userEvent.setup().click(launcher());
    const link = await screen.findByRole("button", { name: "see Health" }, { timeout: 5000 });
    // Ready by the time Health reads: the row is not shown to a non-platform admin.
    assistantCheck = { ok: true, required: false };
    // With the clock held, the arrival's wait for the row cannot run out: the focus reaching
    // the h1 is Health having answered that it shows no row.
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now());
    try {
      await userEvent.setup().click(link);
      await until(() => document.activeElement?.tagName === "H1" && document.activeElement.textContent === "Health");
    } finally {
      clock.mockRestore();
    }
    expect(document.querySelector('[data-focus-key="health-assistant"]')).toBeNull();
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
