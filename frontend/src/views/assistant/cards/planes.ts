// The page that owns each plane a mentor hint names (`MentorHint.plane`, the server's
// mentor_rules.PLANES): a plane gets a link when a page owns it, and none only when no page
// does. Every plane the server reports is listed here, to a page or to null
// (planes.test.ts holds the list to tests/unit/snapshots/mentor_planes.json).
import type { View } from "../../../lib/state";

export interface PlanePage {
  view: View;
  /** What the link says after "Open in". */
  page: string;
  query?: Record<string, string>;
  /** Linked only for a namespace admin (an admin page). */
  admin?: boolean;
  /** The element the arrival focuses once it renders, else the page's heading. */
  arriveFocus?: string;
}

export const PLANE_PAGES: Record<string, PlanePage | null> = {
  claims: { view: "memories", page: "Memories" },
  documents: { view: "knowledge", page: "Library" },
  graph: { view: "graph", page: "Graph" },
  "code index": { view: "code", page: "Repositories" },
  tracker: { view: "code", page: "Repositories", arriveFocus: "trackers-heading" },
  records: { view: "setup", page: "Settings", query: { tab: "structure" } },
  runs: { view: "runs", page: "Runs" },
  conversations: { view: "sessions", page: "Conversations" },
  profile: { view: "profile", page: "Profile" },
  conflicts: { view: "admin:review_queue", page: "Review queue", admin: true },
  // Erasures are a tab of Activity, which no link opens: Activity opens on its events.
  erasure: null,
  scratchpad: null,
  checkpoints: null,
  decisions: null,
  procedures: null,
  namespace: null,
};

/** The page a hint's plane links to for this person, or null. */
export function planePage(plane: string | null | undefined, isAdmin: boolean): PlanePage | null {
  const p = plane ? PLANE_PAGES[plane] : null;
  if (!p || (p.admin && !isAdmin)) return null;
  return p;
}
