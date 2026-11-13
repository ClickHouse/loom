import { useLayoutEffect, useState } from "react";

/** <main>'s width the Library's tree needs: its width at a 1280 px window with the sidebar open
 *  and Ask AI closed. Ask AI beside the page narrows it. */
export const TREE_PAGE_MIN = 1036;

export interface PageRoom {
  /** <main> has room for the tree beside the page. */
  wide: boolean;
  /** The height of <main>'s view. */
  height: number;
}

/** What <main> leaves the Library, kept current as the window, the sidebar or Ask AI change it;
 *  null until it can be measured. The width leaves a scrollbar out, so a list long enough to
 *  scroll never takes the tree away. */
export function usePageRoom(): PageRoom | null {
  const [room, setRoom] = useState<PageRoom | null>(null);
  // Before the first paint, so a cold load draws the right layout at once.
  useLayoutEffect(() => {
    const main = document.getElementById("main");
    if (!main || typeof ResizeObserver === "undefined") return;
    const measure = () => {
      const width = main.getBoundingClientRect().width;
      // Not laid out (no layout engine, or a hidden tab): nothing measured yet.
      if (!width) return;
      const wide = width >= TREE_PAGE_MIN;
      const height = main.clientHeight;
      setRoom((prev) => (prev && prev.wide === wide && prev.height === height ? prev : { wide, height }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(main);
    return () => ro.disconnect();
  }, []);
  return room;
}
