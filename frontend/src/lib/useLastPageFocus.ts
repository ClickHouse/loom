import { useEffect, useRef } from "react";

export interface LastPageFocusOptions {
  /** A Load more read is in flight. */
  loading: boolean;
  /** Another page is left, so the button stays. */
  more: boolean;
  /** The rows shown, by name, each with the control that takes the focus. */
  rows: () => Array<[string, HTMLElement]>;
  /** Where focus goes when no row is shown (a search hides them all). */
  fallback: () => HTMLElement | null;
}

/**
 * The last page of a list read with Load more removes the button, which held the focus
 * (`ui/BusyButton` keeps it through the read). Call the returned `mark` with the names
 * shown before the read; once it ends with no page left, focus that fell to the page goes
 * to the first row the page added, else the last row shown, else `fallback`. Only the
 * render where the read ends moves focus.
 */
export function useLastPageFocus({
  loading,
  more,
  rows,
  fallback,
}: LastPageFocusOptions): (shown: Iterable<string>) => void {
  const before = useRef<Set<string> | null>(null);
  useEffect(() => {
    const had = before.current;
    if (had === null || loading) return;
    before.current = null;
    if (more || document.activeElement !== document.body) return;
    const all = rows();
    const target = all.find(([name]) => !had.has(name))?.[1] ?? all[all.length - 1]?.[1] ?? fallback();
    target?.focus();
  });
  return (shown) => {
    before.current = new Set(shown);
  };
}
