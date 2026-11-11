// What a question says about the page it is asked from. Page context is on by default and
// shown: each new question carries a chip "Includes: <page>", and "Ask about this" adds the
// item it was pressed on ("Includes: Memories · key user/pref/lang"). Removing the chip
// sends nothing about the page. The question goes with one leading line the agent's tools
// can read (`Context: Memories page, memory user/pref/lang`); the transcript shows the chip.
import type { AskItem } from "../../lib/assistantDock";
import type { View } from "../../lib/state";

/** Each page as its title (or its tab, where one page has several) names it. */
export const PAGE_NAMES: Record<View, string> = {
  memories: "Memories",
  recall: "Recall",
  profile: "Profile",
  knowledge: "Library",
  sharing: "Flow",
  sessions: "Conversations",
  runs: "Runs",
  graph: "Graph",
  assistant: "Assistant",
  help: "Help",
  browse: "Browse",
  code: "Repositories",
  tasks: "Tasks",
  setup: "Settings",
  "admin:review_queue": "Review queue",
  "admin:health": "Health",
  "admin:guardrails": "Guardrails",
  "admin:policies": "Policies",
  "admin:activity": "Activity",
  "admin:usage": "Usage",
  "admin:agents": "Agents",
  "admin:users": "Users",
  "admin:settings": "Deployment settings",
};

/** What one question carries about the page: the chip's words and the line sent with it. */
export interface AskContext {
  chip: string;
  line: string;
}

/** The context the next question carries: the page, and the item "Ask about this" set
 *  while it is still this page's; none once the person removed the chip. */
export function pageContext(view: View, item: AskItem | null, pageOff: boolean): AskContext | null {
  if (pageOff) return null;
  const page = PAGE_NAMES[view];
  const mine = item && item.view === view ? item : null;
  return mine
    ? { chip: `${page} · ${mine.chip}`, line: `Context: ${page} page, ${mine.line}` }
    : { chip: page, line: `Context: ${page} page` };
}

/** The message a question is sent as: the context line first, then the question. */
export function withContext(question: string, context: AskContext | null | undefined): string {
  return context ? `${context.line}\n\n${question}` : question;
}

// The one-time hint under the field: shown until the person removes the chip once or asks
// a first question. A per-viewer convenience: storage refused shows it again next time.
const HINT_KEY = "loom_assistant_page_hint";

export function pageHintSeen(): boolean {
  try {
    return localStorage.getItem(HINT_KEY) === "1";
  } catch {
    return false;
  }
}

export function markPageHintSeen(): void {
  try {
    localStorage.setItem(HINT_KEY, "1");
  } catch {
    /* storage refused: the hint shows again next time */
  }
}
