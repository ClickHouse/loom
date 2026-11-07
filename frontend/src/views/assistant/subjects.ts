import type { InspectSubject, InventoryPopulationName } from "../../api";
import { displayAgent } from "../../lib/format";

// What the page calls each subject: its toggle label, its id field's label, and the Inspect
// button's reason while the field is empty, with names suggested above it (`pick`) and
// without (`empty`). A session is never suggested, so its two are the same.
export const SUBJECT_COPY: Record<InspectSubject, { label: string; field: string; empty: string; pick: string }> = {
  user: { label: "User", field: "User id", empty: "Type a user id.", pick: "Pick a user above or type an id." },
  agent: {
    label: "Agent",
    field: "Agent name",
    empty: "Type an agent name.",
    pick: "Pick an agent above or type its name.",
  },
  session: { label: "Session", field: "Session id", empty: "Type a session id.", pick: "Type a session id." },
};

// The subject each listed population is inspected as; sessions are not listed.
export const INSPECTED_AS: Partial<Record<InventoryPopulationName, InspectSubject>> = {
  subjects: "user",
  agents: "agent",
};

/** How the page shows an id of `subject`: an agent by the name the dashboard gives it. */
export const shownName = (subject: InspectSubject, id: string) => (subject === "agent" ? displayAgent(id) : id);
