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

/** Open the Explorer on `entity` in this tab, as a new history entry: Back returns to the
 *  page it was opened from, with its scroll, its open memory and its focus. `from` names
 *  what the person was reading ("memory api/rate-limit") — the Graph then offers "Back to
 *  <from>" — and `focusKey` the element Back focuses (the entity chip). The Explorer reads
 *  `?seed=` on mount and walks without a further gesture. */
export function openGraphAt(entity: string, opts: { from?: string; focusKey?: string } = {}): void {
  const seed = entity.trim().toLowerCase();
  useStore.getState().navigate("graph", {
    query: seed ? { seed } : {},
    back: opts.from,
    focusKey: opts.focusKey,
    note: seed ? `Graph, walked from ${seed}` : undefined,
  });
}

/** Whether a click on an anchor is the browser's own (cmd, ctrl, shift, middle: a new tab
 *  or window), not one to handle in place. */
export function isModifiedClick(e: { metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; button: number }): boolean {
  return e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1;
}

/** Anchor click handler: plain click opens in place, modified clicks (cmd,
 *  ctrl, shift, middle) keep the browser's own new-tab behaviour. */
export function onGraphLinkClick(entity: string, opts: { from?: string; focusKey?: string } = {}) {
  return (e: {
    metaKey: boolean;
    ctrlKey: boolean;
    shiftKey: boolean;
    button: number;
    preventDefault: () => void;
  }): void => {
    if (isModifiedClick(e)) return;
    e.preventDefault();
    openGraphAt(entity, opts);
  };
}
