import { useEffect, useRef } from "react";
import { focusViewHeading } from "./place";

/** The Library tree's landmark name, which the focus handoff finds it by. */
export const LIBRARY_PLACES_LABEL = "Library places";

/** The place picker's name, shown where no tree is: the page hands it the focus when the tree goes. */
export const PLACE_PICKER_LABEL = "Show the documents of";

/** Where the focus goes: the tree's current row (else its first) when it is drawn, the place
 *  picker when it goes. */
function handoffTarget(besideTree: boolean): HTMLElement | null {
  if (!besideTree) return document.querySelector<HTMLElement>(`[aria-label="${PLACE_PICKER_LABEL}"]`);
  const tree = document.querySelector(`nav[aria-label="${LIBRARY_PLACES_LABEL}"]`);
  return tree?.querySelector<HTMLElement>('a[aria-current="page"]') ?? tree?.querySelector<HTMLElement>("a") ?? null;
}

/** When `besideTree` changes (a resize draws or removes the Library's tree) and the last
 *  control focused in <main> went with it, the focus goes on the next frame to `handoffTarget`,
 *  else the view's h1. Focus still on the page, or since moved out of <main>, stays. A title
 *  typed in the tree's New collection form goes with the tree. A click on the page's
 *  background fires no focusin, so the control before it still counts as last and a resize
 *  after it hands the focus on. */
export function useTreeFocusHandoff(besideTree: boolean): void {
  const last = useRef<Element | null>(null);
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      // A popper (the picker's list and its search) is portalled out of <main> but belongs to
      // the control that opened it: that control stays the one to hand on.
      if (e.target instanceof Element && e.target.closest("[data-radix-popper-content-wrapper]")) return;
      // Focus that leaves <main> otherwise (a dialog, the header) is not this hook's to hand back.
      last.current = e.target instanceof Element && e.target.closest("#main") ? e.target : null;
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, []);
  const seen = useRef(besideTree);
  useEffect(() => {
    if (seen.current === besideTree) return;
    seen.current = besideTree;
    const frame = requestAnimationFrame(() => {
      const active = document.activeElement;
      if ((active && active !== document.body) || !last.current || last.current.isConnected) return;
      const to = handoffTarget(besideTree);
      if (to) to.focus();
      else focusViewHeading();
    });
    return () => cancelAnimationFrame(frame);
  }, [besideTree]);
}
