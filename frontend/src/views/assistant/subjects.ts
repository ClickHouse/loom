import type { InspectSubject, InventoryPopulationName } from "../../api";

// What the page calls each subject: its toggle label, its id field's label, and the Inspect
// button's reason while the field is empty. `pick` is that reason for a subject the
// overview suggests names for (SUBJECT_OF); a session is never suggested.
export const SUBJECTS: Record<InspectSubject, { label: string; field: string; empty: string; pick?: string }> = {
  user: { label: "User", field: "User id", empty: "Type a user id.", pick: "Pick a user above or type an id." },
  agent: {
    label: "Agent",
    field: "Agent name",
    empty: "Type an agent name.",
    pick: "Pick an agent above or type its name.",
  },
  session: { label: "Session", field: "Session id", empty: "Type a session id." },
};

// The subject each listed population can be inspected as; sessions are not listed.
export const SUBJECT_OF: Partial<Record<InventoryPopulationName, InspectSubject>> = {
  subjects: "user",
  agents: "agent",
};
