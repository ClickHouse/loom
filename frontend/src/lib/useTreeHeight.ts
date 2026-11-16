import { useLayoutEffect, useState } from "react";

/** <main>'s width the Library's tree needs: its width at a 1280 px window with the sidebar open
 *  and Ask AI closed. Ask AI beside the page narrows it. */
export const TREE_PAGE_MIN = 1036;

/** The height the Library's tree takes (<main>'s view) where <main> has room for it beside the
 *  page, else null; kept current as the window, the sidebar or Ask AI change it. Measured with
 *  the scrollbar, so a list long enough to scroll never takes the tree away. */
export function useTreeHeight(): number | null {
  const [height, setHeight] = useState<number | null>(null);
  // Before the first paint, so a cold load draws the right layout at once.
  useLayoutEffect(() => {
    const main = document.getElementById("main");
    if (!main) return;
    const measure = () => setHeight(main.getBoundingClientRect().width >= TREE_PAGE_MIN ? main.clientHeight : null);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(main);
    return () => ro.disconnect();
  }, []);
  return height;
}
