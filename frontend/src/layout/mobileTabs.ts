import type { View } from "../lib/state";

/** The bottom nav's page tabs; every other page is in its "More" sheet. */
export const PRIMARY_IDS: View[] = ["memories", "knowledge", "runs"];

/** The current tab and sheet entry use the rail's selected fill. */
export const ACTIVE = "workspace-nav-current";

export function primaryView(view: View): View {
  return view === "recall" ? "memories" : view;
}
