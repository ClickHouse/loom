// The starters: one per question Loom's own reads answer exactly. Each asks the assistant
// its question (when "Also ask the assistant" is on and the assistant can answer) and reads
// its card with the person's token. The question names the arguments the card was first
// read with, so the prose and that read are about the same thing; the card's own selects
// read it again with others, which the question then no longer names.
import type { InspectSubject } from "../../../api";
import type { CardArgs, CardOp } from "./types";

export type StarterId = "inventory" | "audit" | "subject" | "documents" | "mentor" | "graph" | "tune";

export interface Starter {
  id: StarterId;
  op: CardOp;
  /** What the button says. */
  label: string;
  /** What the card is called in its heading and its announcement. */
  title: string;
}

/** In the order the empty state lists them; the phone sheet shows the first four. */
export const STARTERS: readonly Starter[] = [
  {
    id: "inventory",
    op: "namespace.inventory",
    label: "What does this namespace hold, and who appears in it?",
    title: "Inventory",
  },
  { id: "audit", op: "namespace.audit", label: "How is this namespace being used?", title: "Audit" },
  { id: "subject", op: "memory.inspect", label: "What does a user, agent or session remember?", title: "Memories" },
  { id: "documents", op: "knowledge.inspect", label: "What documents does it hold?", title: "Documents" },
  { id: "mentor", op: "mentor.brief", label: "What would Loom advise an agent writing here?", title: "Mentor" },
  { id: "graph", op: "graph", label: "How are an entity and its neighbours related?", title: "Relationships" },
  { id: "tune", op: "namespace.tune", label: "What could be improved here?", title: "Improvements" },
];

/** The starters the phone sheet shows before "More starters". */
export const PHONE_STARTERS = 4;

export const DAY = 86_400;
export const AUDIT_WINDOWS: Array<{ label: string; seconds: number }> = [
  { label: "1 day", seconds: DAY },
  { label: "7 days", seconds: 7 * DAY },
  { label: "30 days", seconds: 30 * DAY },
];
export const MENTOR_WINDOWS = AUDIT_WINDOWS.slice(1);
export const DEFAULT_WINDOW = 7 * DAY;

export const starterOf = (op: CardOp): Starter => STARTERS.find((s) => s.op === op) ?? STARTERS[0];

const days = (seconds = DEFAULT_WINDOW) => {
  const n = Math.round(seconds / DAY);
  return n === 1 ? "day" : `${n} days`;
};

const SUBJECT_WORD: Record<InspectSubject, string> = { user: "user", agent: "agent", session: "session" };

/** The question a starter asks, naming the arguments its card reads with. */
export function starterQuestion(op: CardOp, args: CardArgs): string {
  switch (op) {
    case "namespace.inventory":
      return "What does this namespace hold, and who appears in it?";
    case "namespace.audit":
      return `How is this namespace being used over the last ${days(args.windowSeconds)}?`;
    case "mentor.brief":
      return args.agent
        ? `What would Loom advise agent ${args.agent} writing here, over the last ${days(args.windowSeconds)}?`
        : `What would Loom advise the most active agent writing here, over the last ${days(args.windowSeconds)}?`;
    case "memory.inspect":
      return `What does ${SUBJECT_WORD[args.subject ?? "user"]} ${args.id ?? ""} remember?`;
    case "knowledge.inspect":
      return "What documents does this namespace hold?";
    case "graph":
      return args.entity
        ? `How are ${args.entity} and its neighbours related?`
        : "What do the facts in this namespace's memories connect?";
    case "namespace.tune":
      return "What could be improved in this namespace?";
  }
}
