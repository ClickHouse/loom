// The assistant panel's shell state, which every page pays for: whether the panel is open,
// how wide, the mode the window gives it, the launcher's marks, the question being typed,
// and a reason the assistant cannot answer when one is known. What the panel does (open,
// close, the phone sheet's history entry, the inert) loads with it
// (./assistantDockRuntime.ts); the chat and its stored conversations are its body's chunk
// (views/assistant/Dock.tsx).
//
// One panel element serves every mode, and only its position, the trap and the page's
// inert change between them, so a resize keeps the focus and the draft:
//   push     beside <main> while <main> keeps PAGE_MIN px; non-modal.
//   overlay  over the page at the right, full height, a scrim and the page inert; modal.
//   sheet    below PHONE_MIN px wide, full screen with a history entry of its own; modal.
import { create } from "zustand";
import { isAuthDisabled } from "../api";
import { INDEX_KEY, PANEL_KEY } from "./assistantStorage";
import { patchEntry } from "./place";
import { sheetEntry, useStore } from "./state";
import type { AssistantReason } from "./assistantLayout";

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
}

// Auth off is known from the first paint; the token's claims are read when the panel's
// behaviour loads, and the rest when it opens.
const authOff = isAuthDisabled();

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
  reason: authOff ? { kind: "auth_off" } : null,
  reasonShort: authOff ? "needs auth on" : "",
  focusField: false,
}));

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

/** The launcher's click and Ctrl+/ (⌘/): open the panel, or close it. */
export function toggleAssistantSoon(): void {
  void loadDockRuntime().then((m) => m.toggleAssistant());
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
 *  deleted as this one signs in). */
export function bootDock(): void {
  if (sheetEntry()) patchEntry({ loomSheet: null });
  useDock.setState({ viewport: window.innerWidth, collapsed: useStore.getState().sidebarCollapsed });
  let wanted = false;
  try {
    wanted = /"open":true/.test(localStorage.getItem(PANEL_KEY) || "") || localStorage.getItem(INDEX_KEY) !== null;
  } catch {
    /* storage refused: nothing stored */
  }
  if (wanted) void loadDockRuntime().then((m) => m.restorePanel());
}
