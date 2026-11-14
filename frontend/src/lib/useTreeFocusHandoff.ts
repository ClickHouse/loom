import { useEffect, useRef } from "react";
import { focusViewHeading } from "./place";

/** When `besideTree` changes (a resize draws or removes the Library's tree) and the last
 *  control focused in <main> went with it, `target(besideTree)` takes the focus on the next
 *  frame: to the tree's current row when the tree is drawn, to the picker when it goes; else
 *  the view's h1. Focus still on the page, or since moved out of <main>, stays. */
export function useTreeFocusHandoff(besideTree: boolean, target: (besideTree: boolean) => HTMLElement | null): void {
  const last = useRef<Element | null>(null);
  const latest = useRef(target);
  latest.current = target;
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
      const to = latest.current(seen.current);
      if (to) to.focus();
      else focusViewHeading();
    });
    return () => cancelAnimationFrame(frame);
  }, [besideTree]);
}
