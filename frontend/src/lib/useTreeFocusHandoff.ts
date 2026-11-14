import { useEffect, useRef } from "react";
import { focusViewHeading } from "./place";

/** When `besideTree` changes (a resize draws or removes the Library's tree) and the last
 *  control focused in <main> went with it, `target()` takes the focus on the next frame,
 *  else the view's h1. Focus still on the page, or since moved out of <main>, stays. */
export function useTreeFocusHandoff(besideTree: boolean, target: () => HTMLElement | null): void {
  const last = useRef<Element | null>(null);
  const latest = useRef(target);
  latest.current = target;
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      // Focus that leaves <main> (a dialog, the header) is not this hook's to hand back.
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
      const to = latest.current();
      if (to) to.focus();
      else focusViewHeading();
    });
    return () => cancelAnimationFrame(frame);
  }, [besideTree]);
}
