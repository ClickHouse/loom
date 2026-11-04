/**
 * Where a person was on a page, kept in the history entry so Back puts them there again.
 *
 * Leaving a view writes `{ scrollTop, focusKey, inspected }` into the entry being left
 * (`rememberPlace`): how far `<main>` was scrolled, the `data-focus-key` of the element
 * that had focus, and the memory open in the drawer. `popstate` hands that entry back;
 * `restorePlace` scrolls `<main>` and focuses the keyed element once the page has drawn
 * them. A navigation that reaches a new view moves focus to its h1 and says where it went
 * in one polite status (`arriveAt`).
 *
 * Every key this module writes into `history.state` is its own (`loomPlace`, `loomBack`,
 * `loomInspect`), merged over what is there: the Graph keeps its walk in the same state.
 */

/** The user and session a memory row is filed under, when it is opened from a view that
 *  names them: memory.get reads a row by its whole writer identity. */
export interface InspectedOwner {
  user_id: string;
  session_id: string;
}

export interface Place {
  scrollTop: number;
  /** The `data-focus-key` of the element that had focus; null when nothing keyed did. */
  focusKey: string | null;
  /** The memory the drawer showed, and the writer agent it was read as. */
  inspected: string | null;
  inspectedAgent: string;
  /** The user and session it was read under; null for the caller's own. */
  inspectedOwner: InspectedOwner | null;
}

/** How long a restore waits for the page to draw what it restores (a lazy chunk, a list
 *  read, the drawer's memory read). */
const RESTORE_WAIT_MS = 3000;

function stateRecord(): Record<string, unknown> {
  const s: unknown = typeof history === "undefined" ? null : history.state;
  return s && typeof s === "object" ? (s as Record<string, unknown>) : {};
}

/** Merge `patch` into the current history entry's state, keeping its URL unless `url`
 *  is given. */
export function patchEntry(patch: Record<string, unknown>, url?: string): void {
  try {
    history.replaceState({ ...stateRecord(), ...patch }, "", url ?? window.location.href);
  } catch {
    /* history unavailable */
  }
}

function mainElement(): HTMLElement | null {
  return typeof document === "undefined" ? null : document.getElementById("main");
}

/** Write the current place into the entry about to be left. `focusKey` names the element
 *  Back should focus when the focused one is not it (a popover link that opened the jump,
 *  where Back should land on the chip that opened the popover). */
export function rememberPlace(
  inspected: { key: string | null; agent: string; owner?: InspectedOwner | null },
  focusKey?: string | null,
): void {
  if (typeof window === "undefined") return;
  const active = document.activeElement;
  const keyed = active instanceof Element ? active.closest("[data-focus-key]") : null;
  const place: Place = {
    scrollTop: mainElement()?.scrollTop ?? 0,
    focusKey: focusKey ?? keyed?.getAttribute("data-focus-key") ?? null,
    inspected: inspected.key,
    inspectedAgent: inspected.agent,
    inspectedOwner: inspected.owner ?? null,
  };
  patchEntry({ loomPlace: place });
}

/** The place a history entry remembers, or null. */
export function readPlace(state: unknown): Place | null {
  if (!state || typeof state !== "object") return null;
  const p = (state as Record<string, unknown>).loomPlace;
  if (!p || typeof p !== "object") return null;
  const r = p as Record<string, unknown>;
  return {
    scrollTop: typeof r.scrollTop === "number" ? r.scrollTop : 0,
    focusKey: typeof r.focusKey === "string" ? r.focusKey : null,
    inspected: typeof r.inspected === "string" ? r.inspected : null,
    inspectedAgent: typeof r.inspectedAgent === "string" ? r.inspectedAgent : "",
    inspectedOwner: owner(r.inspectedOwner),
  };
}

function owner(v: unknown): InspectedOwner | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  return typeof o.user_id === "string" && typeof o.session_id === "string"
    ? { user_id: o.user_id, session_id: o.session_id }
    : null;
}

/** The label of the entry the current one was reached from ("memory api/rate-limit"), set
 *  by a jump that offers a way back; null on any other entry. */
export function readBackLabel(state: unknown = stateRecord()): string | null {
  if (!state || typeof state !== "object") return null;
  const b = (state as Record<string, unknown>).loomBack;
  return typeof b === "string" && b ? b : null;
}

const FOCUSABLE = "a[href], button, input, select, textarea, [tabindex]";

/** The element a focus key names, or inside it the row's open button (a list row is
 *  keyed; its stretched `data-row-open` button takes the focus, not its checkbox), else
 *  the first focusable. */
function keyedTarget(key: string): HTMLElement | null {
  const el = document.querySelector<HTMLElement>(`[data-focus-key="${CSS.escape(key)}"]`);
  if (!el) return null;
  if (el.matches(FOCUSABLE)) return el;
  return el.querySelector<HTMLElement>("[data-row-open]") ?? el.querySelector<HTMLElement>(FOCUSABLE);
}

function viewHeading(): HTMLElement | null {
  return mainElement()?.querySelector<HTMLElement>("h1") ?? null;
}

/** Run `step` each frame until it returns true or the wait runs out; `onTimeout` then.
 *  The first run is a frame away: the store change that started it has not rendered yet,
 *  and until it has, the h1 in <main> is the old page's. */
function untilDrawn(step: () => boolean, onTimeout: () => void = () => {}): () => void {
  const until = Date.now() + RESTORE_WAIT_MS;
  let frame = 0;
  const tick = () => {
    if (step()) return;
    if (Date.now() > until) {
      onTimeout();
      return;
    }
    frame = requestAnimationFrame(tick);
  };
  frame = requestAnimationFrame(tick);
  return () => cancelAnimationFrame(frame);
}

let cancelPending: (() => void) | null = null;

/** Say `message` in the shell's polite status region. */
export function announce(message: string): void {
  const region = typeof document === "undefined" ? null : document.getElementById("loom-nav-status");
  if (!region) return;
  // Cleared first, so the same words said twice are read twice.
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 50);
}

/** A dialog that holds the focus (an editor a navigation opened) keeps it. */
function focusHeldByDialog(): boolean {
  const a = document.activeElement;
  return a instanceof Element && !!a.closest('[role="dialog"], [role="alertdialog"]');
}

function focusHeading(h1: HTMLElement): void {
  if (!h1.hasAttribute("tabindex")) h1.setAttribute("tabindex", "-1");
  h1.focus({ preventScroll: true });
}

/** A new view: once its h1 is drawn, focus it (unless a dialog holds the focus) and say
 *  where the navigation went — `note`, or the h1's text. */
export function arriveAt(note?: string | null): void {
  cancelPending?.();
  cancelPending = untilDrawn(() => {
    const h1 = viewHeading();
    if (!h1) return false;
    if (!focusHeldByDialog()) focusHeading(h1);
    announce(note || h1.textContent || "");
    return true;
  });
}

/** Back or Forward onto `place`: scroll `<main>` back once the page is tall enough, then
 *  focus the keyed element (waiting for it — the drawer reads its memory first), else, when
 *  the entry keyed none, the view's h1, on the same view too. */
export function restorePlace(place: Place | null, viewChanged: boolean): void {
  cancelPending?.();
  const main = mainElement();
  if (!place) {
    if (viewChanged) arriveAt();
    return;
  }
  let headingSaid = !viewChanged;
  let focused = false;
  cancelPending = untilDrawn(
    () => {
      let moved = false;
      if (main && main.scrollHeight - main.clientHeight >= place.scrollTop && main.scrollTop !== place.scrollTop) {
        main.scrollTop = place.scrollTop;
        moved = true;
      }
      const h1 = viewHeading();
      if (!headingSaid && h1) {
        announce(h1.textContent || "");
        headingSaid = true;
      }
      if (!focused) {
        const target = place.focusKey ? keyedTarget(place.focusKey) : null;
        if (target) {
          target.focus({ preventScroll: true });
          focused = true;
          moved = true;
        } else if (!place.focusKey && (!viewChanged || h1)) {
          if (h1 && !focusHeldByDialog()) {
            focusHeading(h1);
            moved = true;
          }
          focused = true;
        }
      }
      // Scrolling and focus can resize contained rows: verify their offset next frame.
      return !moved && (!main || main.scrollTop === place.scrollTop) && headingSaid && focused;
    },
    () => {
      // The page never grew as tall as it was (fewer rows now): scroll as far as it goes.
      if (main) main.scrollTop = place.scrollTop;
      const h1 = viewHeading();
      if (!focused && h1 && !focusHeldByDialog() && viewChanged) focusHeading(h1);
    },
  );
}
