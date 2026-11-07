import type { InspectSubject, InventoryPopulationName } from "../../api";

// The subject each listed population can be inspected as; sessions are not listed.
export const SUBJECT_OF: Partial<Record<InventoryPopulationName, InspectSubject>> = {
  subjects: "user",
  agents: "agent",
};
