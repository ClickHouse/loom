// The assistant panel's layout and reasons: read by the panel, its behaviour and its body,
// which load the first time the panel is needed, and by the header search's body (its
// answer card); the shell holds only ./assistantDock.ts.
import { useDock, type DockState } from "./assistantDock";
import { PANEL_KEY } from "./assistantStorage";

export const PANEL_MIN = 360;
export const PANEL_MAX = 720;
export const PANEL_DEFAULT = 420;
/** The width <main> keeps beside the panel in push mode. */
export const PAGE_MIN = 640;
/** From this window width the panel can sit beside the page; narrower, it covers it. */
export const PUSH_MIN = 1280;
/** From this window width the panel can expand into the window over the page. */
export const EXPAND_MIN = 1024;
/** Below this window width the panel is the phone sheet. */
export const PHONE_MIN = 640;
/** One splitter arrow step. */
export const SPLIT_STEP = 16;

/** `expanded`: the window over the page from EXPAND_MIN up, anchored at the right; modal. */
export type DockMode = "push" | "overlay" | "sheet" | "expanded";

/** Why the assistant cannot answer, as the panel and the launcher word it. */
export type AssistantReason =
  | { kind: "auth_off" }
  | { kind: "org_wide" }
  | { kind: "narrowed"; scope: string }
  | { kind: "token_expires_soon"; leftS?: number }
  | { kind: "not_configured" }
  | { kind: "starting" }
  | { kind: "failing" }
  | { kind: "model_calls_off" };

/** The launcher tooltip's few words for a reason. */
export const SHORT_REASON: Record<AssistantReason["kind"], string> = {
  auth_off: "needs auth on",
  org_wide: "this token spans every org",
  narrowed: "this token is narrowed",
  token_expires_soon: "this token expires too soon",
  not_configured: "chat not set up",
  starting: "setting up",
  failing: "setup failed",
  model_calls_off: "model calls are off",
};

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** The mode, used width and largest width the window gives the panel. Push from PUSH_MIN,
 *  where the page keeps at least PAGE_MIN beside at least PANEL_MIN of panel with the
 *  sidebar open (the panel shrinks toward PANEL_MIN first), then overlay (leaving 64 px of
 *  the page in view), then the phone sheet. The sidebar is Sidebar.tsx's, 60 px collapsed
 *  and 244 px open (at every push width). The stored width is a preference. */
export function dockLayout(vw: number, collapsed: boolean, pref: number) {
  if (vw < PHONE_MIN) return { mode: "sheet" as DockMode, width: vw, max: vw };
  const room = vw - (collapsed ? 60 : 244) - PAGE_MIN;
  const push = vw >= PUSH_MIN;
  const max = push ? Math.min(PANEL_MAX, room) : Math.max(PANEL_MIN, Math.min(PANEL_MAX, vw - 64));
  return { mode: (push ? "push" : "overlay") as DockMode, width: clamp(pref, PANEL_MIN, max), max };
}

export function readPanel(): { open: boolean; width: number } {
  try {
    const r = JSON.parse(localStorage.getItem(PANEL_KEY) || "{}") as Record<string, unknown>;
    return { open: r.open === true, width: clamp(Number(r.width) || PANEL_DEFAULT, PANEL_MIN, PANEL_MAX) };
  } catch {
    return { open: false, width: PANEL_DEFAULT };
  }
}

/** The panel's layout: the window's while it is open and expanded, else the docked one. */
export function layoutOf(s: Pick<DockState, "viewport" | "collapsed" | "prefWidth" | "expanded" | "open">) {
  if (s.open && s.expanded) return { mode: "expanded" as DockMode, width: s.viewport, max: s.viewport };
  return dockLayout(s.viewport, s.collapsed, s.prefWidth);
}

/** The one flag every modal behaviour follows: role="dialog", aria-modal, the trap, the
 *  Tab wrap and the page's inert. */
export function isModal(s: DockState): boolean {
  return s.open && !s.expired && layoutOf(s).mode !== "push";
}

export function useDockMode(): DockMode {
  return useDock((s) => layoutOf(s).mode);
}

/** Set the reason the assistant cannot answer, with the launcher's words for it. */
export function setDockReason(reason: AssistantReason | null): void {
  useDock.setState({ reason, reasonShort: reason ? SHORT_REASON[reason.kind] : "" });
}
