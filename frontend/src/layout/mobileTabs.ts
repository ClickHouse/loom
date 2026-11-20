import type { View } from "../lib/state";

/** The bottom nav's page tabs; every other page is in its "More" sheet. */
export const PRIMARY_IDS: View[] = ["memories", "knowledge"];

/** The current tab and sheet entry use the rail's selected fill. */
export const ACTIVE = "bg-(--click-sidebar-main-navigation-item-color-background-active)";
