// The assistant panel's layout and reasons: read by the panel, its behaviour and its body,
// which load the first time the panel is needed (the shell holds only ./assistantDock.ts).
import { isAuthDisabled, storedToken } from "../api";
import { useDock, type DockState } from "./assistantDock";
import { PANEL_KEY } from "./assistantStorage";
import { tokenPayload } from "./boot";

export const PANEL_MIN = 360;
export const PANEL_MAX = 720;
export const PANEL_DEFAULT = 420;
/** The width <main> keeps beside the panel in push mode. */
export const PAGE_MIN = 640;
/** Below this window width the panel is the phone sheet. */
export const PHONE_MIN = 640;
/** One splitter arrow step. */
export const SPLIT_STEP = 16;

/** `expanded`: the /assistant page, where the panel takes <main>'s place at any width. */
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
  not_configured: "not set up",
  starting: "setting up",
  failing: "setup failed",
  model_calls_off: "model calls are off",
};

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** The mode, used width and largest width the window gives the panel. Push while the page
 *  keeps PAGE_MIN beside at least PANEL_MIN of panel (the panel shrinks toward PANEL_MIN
 *  first), then overlay (leaving 64 px of the page in view), then the phone sheet. The
 *  sidebar is Sidebar.tsx's: hidden below md, 60 px collapsed, 244 px open. The stored
 *  width is a preference. */
export function dockLayout(vw: number, collapsed: boolean, pref: number) {
  if (vw < PHONE_MIN) return { mode: "sheet" as DockMode, width: vw, max: vw };
  const room = vw - (vw < 768 ? 0 : collapsed ? 60 : 244) - PAGE_MIN;
  const max = room >= PANEL_MIN ? Math.min(PANEL_MAX, room) : Math.max(PANEL_MIN, Math.min(PANEL_MAX, vw - 64));
  return { mode: (room >= PANEL_MIN ? "push" : "overlay") as DockMode, width: clamp(pref, PANEL_MIN, max), max };
}

export function readPanel(): { open: boolean; width: number } {
  try {
    const r = JSON.parse(localStorage.getItem(PANEL_KEY) || "{}") as Record<string, unknown>;
    return { open: r.open === true, width: clamp(Number(r.width) || PANEL_DEFAULT, PANEL_MIN, PANEL_MAX) };
  } catch {
    return { open: false, width: PANEL_DEFAULT };
  }
}

/** The reason the token's own claims give, read without verification (the route's refusal
 *  stays the truth): auth off, an org-wide token, a read scope narrower than the namespace.
 *  A token that expires soon is the route's to say (a first-party session is refreshed). */
export function claimReason(token: string | null = storedToken()): AssistantReason | null {
  if (isAuthDisabled()) return { kind: "auth_off" };
  const c = tokenPayload(token);
  if (!c) return null;
  if (c.org === "*") return { kind: "org_wide" };
  const pinned = typeof c.read_user_id === "string" ? c.read_user_id.trim() : "";
  if (c.read_scope === "own_session") return { kind: "narrowed", scope: "one session" };
  if (c.read_scope === "own_user" || pinned) return { kind: "narrowed", scope: pinned ? `user ${pinned}` : "one user" };
  return null;
}

export function layoutOf(s: Pick<DockState, "viewport" | "collapsed" | "prefWidth" | "expanded">) {
  if (s.expanded) return { mode: "expanded" as DockMode, width: s.viewport, max: s.viewport };
  return dockLayout(s.viewport, s.collapsed, s.prefWidth);
}

/** The one flag every modal behaviour follows: role="dialog", aria-modal, the trap, the
 *  Tab wrap and the page's inert. */
export function isModal(s: DockState): boolean {
  const mode = layoutOf(s).mode;
  return s.open && !s.expired && mode !== "push" && mode !== "expanded";
}

export function useDockMode(): DockMode {
  return useDock((s) => layoutOf(s).mode);
}

/** Set the reason the assistant cannot answer, with the launcher's words for it. */
export function setDockReason(reason: AssistantReason | null): void {
  useDock.setState({ reason, reasonShort: reason ? SHORT_REASON[reason.kind] : "" });
}
