import { type MutableRefObject, useRef, useState } from "react";
import { useLastPageFocus } from "./useLastPageFocus";

/** How many more each Show more adds. */
export const MORE_SHOWN = 50;

/** A list drawn a step at a time (`useShowMore`). */
export interface ShowMore<T> {
  /** The items drawn now. */
  shown: T[];
  /** The list's element, where the focus goes after a press. */
  list: MutableRefObject<HTMLUListElement | null>;
  /** Show more's label (what a press adds), or null once all are shown. */
  moreLabel: string | null;
  showMore: () => void;
  /** More than the first are shown: Show fewer goes back to them. */
  fewer: boolean;
  showFewer: () => void;
}

/** How a list's items are told apart, and its rows found with the control each focuses. */
export interface ShowMoreRows<T> {
  key: (item: T) => string;
  rows: (list: HTMLUListElement | null) => Array<[string, HTMLElement]>;
}

/** The first `first` items, each Show more adding MORE_SHOWN; `extra` (one of `items`) is drawn
 *  past them wherever it sits. `moreLabel` is the button's label, null once all are shown. The
 *  last press removes its button: the focus goes to the first row it added, else the last row
 *  shown, else `fallback`. */
export function useShowMore<T>(
  items: T[],
  first: number,
  how: ShowMoreRows<T>,
  extra: T | null = null,
  fallback: () => HTMLElement | null = () => null,
): ShowMore<T> {
  const [count, setCount] = useState(first);
  const list = useRef<HTMLUListElement | null>(null);
  const shownAt = (n: number) => {
    const head = items.slice(0, n);
    return extra && !head.includes(extra) ? [...head, extra] : head;
  };
  const shown = shownAt(count);
  // What the next press adds: `extra` may already be drawn among it.
  const adds = shownAt(count + MORE_SHOWN).length - shown.length;
  const markShown = useLastPageFocus({
    loading: false,
    more: adds > 0,
    rows: () => how.rows(list.current),
    fallback,
  });
  return {
    shown,
    list,
    moreLabel: adds > 0 ? `Show ${adds} more` : null,
    showMore: () => {
      markShown(shown.map(how.key));
      setCount(count + MORE_SHOWN);
    },
    fewer: count > first,
    showFewer: () => setCount(first),
  };
}
