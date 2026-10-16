/**
 * The Graph is a drill-down, not a destination. It left the sidebar; the way in is an
 * entity on something you are already reading (a memory's entities, a related memory's
 * shared entities). These helpers build that link so every entry point lands the Explorer
 * on the same `/graph?seed=<entity>` walk (`seed` is the Explorer's own query key).
 */
import { useStore } from "./state";

const GRAPH_PATH = "/graph";

/** The href a seed walk lives at — for anchors, so cmd-click opens a tab. */
export function graphSeedHref(entity: string): string {
  const seed = entity.trim().toLowerCase();
  if (!seed) return GRAPH_PATH;
  const params = new URLSearchParams();
  params.set("seed", seed);
  return `${GRAPH_PATH}?${params.toString()}`;
}

/** Open the Explorer on `entity` in this tab. The seed is written into the
 *  current history entry first so `navigationHref` (which keeps `seed` for
 *  the graph view only) carries it across the view change; the Explorer
 *  reads `?seed=` on mount and walks without a further gesture. */
export function openGraphAt(entity: string): void {
  const seed = entity.trim().toLowerCase();
  try {
    const params = new URLSearchParams(window.location.search);
    if (seed) params.set("seed", seed);
    else params.delete("seed");
    params.delete("path");
    params.delete("mode");
    const qs = params.toString();
    history.replaceState(null, "", `${window.location.pathname}${qs ? `?${qs}` : ""}`);
  } catch {
    /* history unavailable (tests, SSR) */
  }
  useStore.getState().setView("graph");
}

/** Anchor click handler: plain click opens in place, modified clicks (cmd,
 *  ctrl, shift, middle) keep the browser's own new-tab behaviour. */
export function onGraphLinkClick(entity: string) {
  return (e: {
    metaKey: boolean;
    ctrlKey: boolean;
    shiftKey: boolean;
    button: number;
    preventDefault: () => void;
  }): void => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
    e.preventDefault();
    openGraphAt(entity);
  };
}
