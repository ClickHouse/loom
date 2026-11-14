import { useEffect, useRef } from "react";
import { focusViewHeading } from "./place";

/** When `layout` changes (a resize draws or takes away the Library's tree) and the control
 *  that had the focus in <main> went with it, `target()` takes the focus on the next frame,
 *  else the view's h1. Focus still on the page, or moved elsewhere meanwhile, stays. */
export function useLayoutFocusHandoff(layout: unknown, target: () => HTMLElement | null): void {
  const last = useRef<Element | null>(null);
  const latest = useRef(target);
  latest.current = target;
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      if (e.target instanceof Element && e.target.closest("#main")) last.current = e.target;
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, []);
  const seen = useRef(layout);
  useEffect(() => {
    if (Object.is(seen.current, layout)) return;
    seen.current = layout;
    const frame = requestAnimationFrame(() => {
      const active = document.activeElement;
      if ((active && active !== document.body) || !last.current || last.current.isConnected) return;
      const to = latest.current();
      if (to) to.focus();
      else focusViewHeading();
    });
    return () => cancelAnimationFrame(frame);
  }, [layout]);
}
