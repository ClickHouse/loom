// The assistant panel's behaviour, loaded with the panel the first time it is needed (a
// click on the launcher, the shortcut, a reload that restores it open, or a browser holding
// stored conversations): opening and closing it, the phone sheet's history entry, what
// navigations, Back and resizes do to it, the page's inert while it is modal, and the
// deletion of another person's stored conversations on a sign-in. The state it acts on is
// the shell's (lib/assistantDock.ts).
import { isAuthDisabled } from "../api";
import { useDock, type AskItem } from "./assistantDock";
import { PANEL_KEY } from "./assistantStorage";
import {
  PANEL_MAX,
  PANEL_MIN,
  claimReason,
  dockLayout,
  isModal,
  layoutOf,
  readPanel,
  setDockReason,
} from "./assistantLayout";
import { patchEntry, rememberPlace } from "./place";
import { navigationHref, sheetEntry, useStore } from "./state";
import { forgetOtherPeople } from "../views/assistant/storedIndex";

function writePanel(open: boolean, width: number): void {
  try {
    localStorage.setItem(PANEL_KEY, JSON.stringify({ open, width }));
  } catch {
    /* storage refused: the preference lives for this tab */
  }
}

/** The panel's preferred width, kept for the next visit. */
export function setPanelWidth(width: number): void {
  const w = Math.min(PANEL_MAX, Math.max(PANEL_MIN, Math.round(width)));
  useDock.setState({ prefWidth: w });
  writePanel(useDock.getState().open, w);
}

const PANEL_SELECTOR = "[data-assistant-panel]";
const LAUNCHER_SELECTOR = '[data-focus-key="assistant-launcher"]';

function panelElement(): HTMLElement | null {
  return typeof document === "undefined" ? null : document.querySelector<HTMLElement>(PANEL_SELECTOR);
}

function focusInPanel(): boolean {
  const a = typeof document === "undefined" ? null : document.activeElement;
  return a instanceof Element && !!a.closest(PANEL_SELECTOR);
}

function focusLauncher(): void {
  document.querySelector<HTMLElement>(LAUNCHER_SELECTOR)?.focus();
}

/** Open the panel, the focus going to its question field. On the phone the sheet pushes a
 *  history entry of its own (stamped `loomSheet`), so Back closes it; the entry under it
 *  first remembers the place, with the launcher as the element Back focuses. */
export function openAssistant(): void {
  const s = useDock.getState();
  if (s.open) {
    useDock.setState({ focusField: true });
    return;
  }
  if (layoutOf(s).mode === "sheet" && !sheetEntry()) {
    rememberPlace({ key: null, agent: "" }, "assistant-launcher");
    try {
      const st: unknown = history.state;
      history.pushState({ ...(st && typeof st === "object" ? st : {}), loomSheet: true }, "", window.location.href);
      currentSheet = true;
    } catch {
      /* history unavailable: the sheet closes by its button */
    }
  }
  useDock.setState({ open: true, mounted: true, unread: false, focusField: true });
  writePanel(true, s.prefWidth);
}

/** Close the panel. `person`: Esc, Close, the launcher or the shortcut; the focus goes back
 *  to the launcher when it was in the panel, and the phone sheet's own entry is left by
 *  Back. `nav`: a navigation or Back/Forward closed it; the modal attributes go at once, in
 *  the same task, so the arrival's focus is not held by a dialog that is closing, and the
 *  focus is the arrival's to place. */
export function closeAssistant(cause: "person" | "nav"): void {
  const s = useDock.getState();
  if (!s.open) return;
  const wasInside = focusInPanel();
  if (cause === "nav") {
    const panel = panelElement();
    panel?.removeAttribute("role");
    panel?.removeAttribute("aria-modal");
    panel?.removeAttribute("data-open");
  }
  useDock.setState({ open: false, focusField: false });
  writePanel(false, s.prefWidth);
  if (cause === "person") {
    if (sheetEntry()) {
      leavingSheet = true;
      history.back();
    }
    if (wasInside) focusLauncher();
  }
}

/** "Ask about this": the next question names `item` beside the page, and the panel opens
 *  in the mode the width gives with its field focused; nothing is sent, and the question
 *  being typed stays. The panel's body starts a new conversation when the one shown is
 *  about another namespace (views/assistant/Dock.tsx). */
export function askAbout(item: AskItem): void {
  useDock.setState((s) => ({ item, pageOff: false, askSeq: s.askSeq + 1 }));
  openAssistant();
}

/** The launcher and Ctrl+/ (⌘/): open the panel, or close it (a close from outside the
 *  panel leaves the focus where it is); on /assistant, collapse it. */
export function toggleAssistant(): void {
  if (useDock.getState().expanded) collapseAssistant();
  else if (useDock.getState().open) closeAssistant("person");
  else openAssistant();
}

/** The Memories page's Insights button, and an old link to its Insights tab (`audit`). */
export function openStarters(audit: boolean): void {
  useDock.setState((s) => ({ startersSeq: (s.startersSeq ?? 0) + 1, startersAudit: audit }));
  openAssistant();
}

/** The view is /assistant (`on`) or another page (lib/assistantDock.ts showExpanded). */
export function showExpanded(on: boolean): void {
  useDock.setState(on ? { expanded: true, mounted: true, focusField: true } : { expanded: false });
}

// Set while Collapse's own history.back() is on its way: its arrival restores nothing, the
// panel focusing its field.
let collapsing = false;

/** Expand: the panel becomes the /assistant page (the conversation and the draft stay). */
export function expandAssistant(): void {
  useStore.getState().navigate("assistant");
}

/** Collapse: back to the page /assistant was opened from (Back, when the app made the entry),
 *  else Memories in its place, then the panel opens there as the launcher opens it, in the
 *  mode the width gives with its field focused (on the phone, the sheet with its own history
 *  entry, so Back closes only the sheet). */
export function collapseAssistant(): void {
  // Out of the expanded mode at once, so the open below goes by the width.
  useDock.setState({ expanded: false, open: false, mounted: true });
  const st: unknown = history.state;
  if (st && typeof st === "object" && (st as Record<string, unknown>).loomFromApp === true) {
    collapsing = true;
    history.back();
    return;
  }
  history.replaceState({ loomFromApp: true }, "", navigationHref("memories", {}));
  useStore.getState().arriveFromHistory("memories", null);
  openAssistant();
}

// Whether the current entry is the sheet's, as the shell last saw it (Back has already
// replaced history.state by the time popstate runs).
let currentSheet = typeof history !== "undefined" && sheetEntry();

// Set while the panel's own history.back() is on its way: the popstate it causes is the
// panel's, not the person's Back.
let leavingSheet = false;

/** Back or Forward arrived at an entry with `state`. On the phone the sheet is open exactly
 *  when that entry is the sheet's; an overlay closes; a pushed panel stays. Answers whether
 *  the entry left was the sheet's, whose arrival restores nothing: the panel placed the
 *  focus itself (the launcher, after the sheet closes). */
export function assistantPopstate(state: unknown): { leftSheet: boolean } {
  if (collapsing) {
    // Collapse's Back has arrived on the page /assistant was opened from: the panel opens there.
    collapsing = false;
    openAssistant();
    return { leftSheet: true };
  }
  const ownBack = leavingSheet;
  leavingSheet = false;
  const left = currentSheet;
  const stamped = !!state && typeof state === "object" && (state as Record<string, unknown>).loomSheet === true;
  currentSheet = stamped;
  const s = useDock.getState();
  const mode = layoutOf(s).mode;
  if (mode === "sheet") {
    if (stamped && !s.open) useDock.setState({ open: true, mounted: true, unread: false, focusField: true });
    else if (!stamped && s.open) {
      const inside = focusInPanel();
      closeAssistant("nav");
      if (inside) focusLauncher();
    } else if (left && !s.open && !ownBack) focusLauncher();
  } else if (mode === "overlay" && s.open) closeAssistant("nav");
  return { leftSheet: left };
}

/** Say `message` in the assistant's own polite status region (App renders it from the first
 *  paint, outside the panel, so it is never inert or aria-hidden). */
export function announceAssistant(message: string): void {
  const region = typeof document === "undefined" ? null : document.getElementById("loom-assistant-status");
  if (!region) return;
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 50);
}

/** The page elements a modal panel makes inert (marked `data-dock-inert` in App and its
 *  layout): the skip link, the sidebar, the reload bar, the header, <main> and the phone
 *  nav. The toasts, portals and live regions stay live. */
function applyInert(modal: boolean): void {
  if (typeof document === "undefined") return;
  for (const el of document.querySelectorAll<HTMLElement>("[data-dock-inert]")) {
    if (modal) el.setAttribute("inert", "");
    else el.removeAttribute("inert");
  }
}

/** Set up the wiring once, when this module loads: the window width, the sidebar, the
 *  session, navigations, the inert and the stored conversations' owner. */
export function installDock(): void {
  currentSheet = sheetEntry();
  useDock.setState({
    viewport: window.innerWidth,
    collapsed: useStore.getState().sidebarCollapsed,
    prefWidth: readPanel().width,
  });
  // The token's own claims: an org-wide or narrowed token cannot ask.
  const claimed = claimReason();
  if (claimed) setDockReason(claimed);
  const onResize = () => {
    const before = useDock.getState();
    const was = layoutOf(before).mode;
    useDock.setState({ viewport: window.innerWidth });
    const now = layoutOf(useDock.getState()).mode;
    // An open sheet that becomes a docked panel keeps its history entry, unstamped: Back from
    // it arrives at the entry under it (the same page), whose place App restores, and the
    // docked panel stays open.
    if (was === "sheet" && now !== "sheet" && sheetEntry()) {
      patchEntry({ loomSheet: null });
      currentSheet = false;
    }
  };
  window.addEventListener("resize", onResize);
  const owner = (w: ReturnType<typeof useStore.getState>["whoami"]) => {
    // Auth off stores nothing: there is no person to key it by.
    if (isAuthDisabled() || !w?.subject) return;
    forgetOtherPeople(w.subject, w.org ?? "");
  };
  owner(useStore.getState().whoami);
  useStore.subscribe((s, prev) => {
    if (s.whoami !== prev.whoami) owner(s.whoami);
    if (s.sidebarCollapsed !== prev.sidebarCollapsed) useDock.setState({ collapsed: s.sidebarCollapsed });
    if (s.sessionExpired !== prev.sessionExpired) {
      useDock.setState({ expired: s.sessionExpired });
      // The session card must be reachable: a modal panel closes (its state stays).
      if (s.sessionExpired && layoutOf(useDock.getState()).mode !== "push") closeAssistant("nav");
    }
    // The item "Ask about this" set belongs to the page, org and namespace it was asked
    // from, and to the person who asked; a removed chip comes back on another page, which it
    // never named.
    const item = useDock.getState().item;
    if (
      item &&
      (s.view !== item.view ||
        (s.identity?.org ?? "") !== item.org ||
        (s.identity?.namespace ?? "") !== item.namespace ||
        s.whoami !== prev.whoami)
    )
      useDock.setState({ item: null });
    if (s.view !== prev.view && useDock.getState().pageOff) useDock.setState({ pageOff: false });
    if (s.navSeq !== prev.navSeq) {
      // A navigation from the sheet replaced the sheet's entry.
      currentSheet = sheetEntry();
      if (layoutOf(useDock.getState()).mode !== "push") closeAssistant("nav");
    }
  });
  let modal = isModal(useDock.getState());
  applyInert(modal);
  useDock.subscribe((s) => {
    const now = isModal(s);
    if (now !== modal) {
      modal = now;
      applyInert(now);
    }
  });
}

/** Reopen the panel a reload finds left open, only where it would push (a modal on every
 *  reload would block the page), taking no focus. */
export function restorePanel(): void {
  const saved = readPanel();
  const s = useDock.getState();
  if (saved.open && !s.open && dockLayout(window.innerWidth, s.collapsed, saved.width).mode === "push")
    useDock.setState({ open: true, mounted: true, focusField: false });
}
