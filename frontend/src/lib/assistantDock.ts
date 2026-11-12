// The assistant panel's shell state, which every page pays for: whether the panel is open,
// how wide, the mode the window gives it, the launcher's marks, the question being typed,
// and a reason the assistant cannot answer when one is known. What the panel does (open,
// close, expand, the phone sheet's history entry, the inert) loads with it
// (./assistantDockRuntime.ts); the chat and its stored conversations are its body's chunk
// (views/assistant/Dock.tsx). It opens from the assistant edge at the window's right from
// md, and from the bottom navigation below md (App.tsx, layout/MobileNav.tsx).
//
// One panel element serves every mode, and only its position, the trap and the page's
// inert change between them, so a resize keeps the focus and the draft:
//   push     beside <main> while <main> keeps PAGE_MIN px; non-modal.
//   overlay  over the page at the right, full height, a scrim and the page inert; modal.
//   sheet    below PHONE_MIN px wide, full screen with a history entry of its own; modal.
//   expanded a window over the page from EXPAND_MIN px, anchored at the right; modal. It
//            changes neither the address nor the history, and a reload never restores it.
import { create } from "zustand";
import { isAuthDisabled } from "../api";
import { INDEX_KEY, PANEL_KEY } from "./assistantStorage";
import { patchEntry } from "./place";
import { pathnameView, sheetEntry, urlParam, useStore, type View } from "./state";
import type { AssistantReason } from "./assistantLayout";

/** The thing "Ask about this" names for the next question: a memory, a document or a
 *  Recall result, on the page and in the namespace it was asked from. */
export interface AskItem {
  view: View;
  org: string;
  namespace: string;
  /** The chip's words after the page ("key user/pref/lang"). */
  chip: string;
  /** The context line's words after the page, which the agent's tools can read: an
   *  identifier (a key, a source URI) written exactly (`exact`), free text (an agent, a
   *  title, a query) clipped and quoted (`quoted`, both in components/AskAboutThis.tsx). */
  line: string;
}

export interface DockState {
  open: boolean;
  /** The panel has opened in this tab: its element and its chunk stay from then on. */
  mounted: boolean;
  prefWidth: number;
  viewport: number;
  collapsed: boolean;
  /** The session ended (the card over the page): a modal panel steps aside for it. */
  expired: boolean;
  /** An answer is streaming. */
  working: boolean;
  /** An answer finished while the panel was closed, not seen yet. */
  unread: boolean;
  /** The question being typed: the panel's, kept across modes and conversations. */
  draft: string;
  /** A reason the assistant cannot answer, from the token's claims, the readiness read or
   *  the chat route's refusal; null when none is known. Never stored. */
  reason: AssistantReason | null;
  /** The launcher tooltip's few words for it. */
  reasonShort: string;
  /** The panel's question field takes the focus once it has drawn. */
  focusField: boolean;
  /** The item "Ask about this" set for the next question; it goes with the page chip. */
  item: AskItem | null;
  /** The person removed the page chip: the next question says nothing about the page. */
  pageOff: boolean;
  /** Bumped by each "Ask about this", so the panel's body can act on it once. */
  askSeq: number;
  // Set only once the panel's behaviour loads (absent reads as false or 0), so the shell
  // carries no default for them.
  /** The open panel is the window over the page; only ever set while open. */
  expanded?: boolean;
  /** Expand found the panel beside the page: <main> keeps a spacer of its width under the
   *  window, so the page does not reflow. */
  expandedFromPush?: boolean;
  /** The panel slides in: it appeared from closed over the page (overlay, sheet, window). */
  entering?: boolean;
  /** Bumped by the Memories page's Insights button: the panel opens on the starters. */
  startersSeq?: number;
  /** The audit's card is read too (an old link to the Memories Insights tab), nothing asked. */
  startersAudit?: boolean;
}

/** Whether this deployment's chat is off (the server's boot snippet): the panel is its
 *  starters, Loom's own reads, and nothing asks a model. A missing flag reads as on. */
export function chatOff(): boolean {
  return window.__LOOM_ASSISTANT === false;
}

// Chat off and auth off are known from the first paint (chat off wins); the token's claims
// are read when the panel's behaviour loads, and the rest when it opens.
const seed: AssistantReason | null = chatOff()
  ? { kind: "not_configured" }
  : isAuthDisabled()
    ? { kind: "auth_off" }
    : null;

export const useDock = create<DockState>(() => ({
  open: false,
  mounted: false,
  // PANEL_DEFAULT (./assistantLayout.ts); the stored preference replaces it as the panel loads.
  prefWidth: 420,
  viewport: 1280,
  collapsed: false,
  expired: false,
  working: false,
  unread: false,
  draft: "",
  reason: seed,
  reasonShort: seed ? (seed.kind === "auth_off" ? "needs auth on" : "chat not set up") : "",
  focusField: false,
  item: null,
  pageOff: false,
  askSeq: 0,
}));

// An old link to the Memories page's Insights tab (`?section=insights`): the boot's address
// leaves the key out, and the panel opens on the audit card once the shell has drawn.
let insightsLink = pathnameView() === "memories" && urlParam("section") === "insights";

/** The address is /assistant (or `?view=assistant`): a bookmarked address, no view. */
export function assistantAddress(): boolean {
  return /^\/assistant\/?$/.test(window.location.pathname) || urlParam("view") === "assistant";
}
// Read before the boot replaces it with the landing page's.
let assistantLink = assistantAddress();

type Runtime = typeof import("./assistantDockRuntime");
let runtime: Runtime | null = null;
let loading: Promise<Runtime> | null = null;

/** The panel's behaviour, loaded once (it wires itself up as it loads). */
export function loadDockRuntime(): Promise<Runtime> {
  loading ??= import("./assistantDockRuntime").then((m) => {
    if (!runtime) m.installDock();
    runtime = m;
    return m;
  });
  return loading;
}

/** The edge's click and Ctrl+/ (⌘/): open the panel, or close it. */
export function toggleAssistantSoon(): void {
  void loadDockRuntime().then((m) => m.toggleAssistant());
}

/** The bottom nav's item and "Go to the assistant": open the panel, or focus it. */
export function openAssistantSoon(): void {
  void loadDockRuntime().then((m) => m.openAssistant());
}

/** Ctrl+/ (⌘/ on Apple keyboards): open or close the panel. A key pressed in another dialog
 *  (the inspector, a confirmation, the help) is left to it. */
export function onAssistantShortcut(e: KeyboardEvent): void {
  if (e.key !== "/" || !(e.ctrlKey || e.metaKey) || e.altKey) return;
  const a = document.activeElement;
  const dialog = a instanceof Element ? a.closest('[role="dialog"], [role="alertdialog"]') : null;
  if (dialog && !dialog.matches("[data-assistant-panel]")) return;
  e.preventDefault();
  toggleAssistantSoon();
}

/** Back or Forward: the panel's say, before App restores the entry's place. True when the
 *  entry left was the phone sheet's, whose arrival restores nothing. An entry of a sheet a
 *  page opened before a reload opens it again once the panel has loaded. */
export function assistantPopstate(state: unknown): boolean {
  if (runtime) return runtime.assistantPopstate(state).leftSheet;
  if (state && typeof state === "object" && (state as Record<string, unknown>).loomSheet === true)
    void loadDockRuntime().then((m) => m.assistantPopstate(state));
  return false;
}

/** The shell has drawn. A page loaded on the sheet's entry (a reload) shows no sheet: the
 *  entry is the page's. The panel's behaviour loads now when the panel was left open (it
 *  reopens where it would push) or the browser holds conversations (another person's are
 *  deleted as this one signs in); an /assistant address opens it over the landing page. */
export function bootDock(): void {
  if (sheetEntry()) patchEntry({ loomSheet: null });
  useDock.setState({ viewport: window.innerWidth, collapsed: useStore.getState().sidebarCollapsed });
  let wanted = false;
  try {
    wanted = /"open":true/.test(localStorage.getItem(PANEL_KEY) || "") || localStorage.getItem(INDEX_KEY) !== null;
  } catch {
    /* storage refused: nothing stored */
  }
  if (insightsLink) {
    insightsLink = false;
    void loadDockRuntime().then((m) => m.openStarters(true));
  } else if (assistantLink) {
    assistantLink = false;
    void loadDockRuntime().then((m) => m.openFromAssistantAddress(false));
  } else if (wanted) void loadDockRuntime().then((m) => m.restorePanel());
}
