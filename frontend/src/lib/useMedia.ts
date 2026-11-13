import { useEffect, useState } from "react";

/** Under Tailwind's `sm` breakpoint: a phone, where a toolbar holds one control. */
export const PHONE_QUERY = "(max-width: 639px)";
/** Wide enough for the Library's tree of places beside its documents, where <main> cannot be
 *  measured (LibraryTree.tsx TREE_PAGE_MIN decides where it can). */
export const TREE_QUERY = "(min-width: 1280px)";

/** A touch screen: the primary pointer is a finger, not a mouse. */
export const COARSE_POINTER = "(pointer: coarse)";

/** The reader asked the OS for less motion. */
export const reducedMotion = (): boolean =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Whether `query` matches, kept current as the viewport changes. */
export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof matchMedia === "function" && matchMedia(query).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(query);
    const sync = () => setOn(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, [query]);
  return on;
}
