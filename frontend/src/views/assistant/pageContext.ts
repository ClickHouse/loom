// What a question says about the page it is asked from. Page context is on by default and
// shown: each new question carries a chip "Includes: <page>", and "Ask about this" adds the
// item it was pressed on ("Includes: Memories · key user/pref/lang"). Removing the chip
// sends nothing about the page. The question goes with one leading line the agent's tools
// can read (`Context: Memories page, memory "user/pref/lang"`); the transcript shows the chip.
import { useDock, type AskItem } from "../../lib/assistantDock";
import { PAGE_HINT_KEY } from "../../lib/assistantStorage";
import { useStore, type View } from "../../lib/state";

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

/** What a question is asked with: the context its chip gave (null once removed) and the
 *  item behind it, which goes back to the chip when the question is not answered. */
export interface Asked {
  context: AskContext | null;
  item: AskItem | null;
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

/** What the chip shows now, as a question would be asked with it. */
export function currentAsk(): Asked {
  const { item, pageOff } = useDock.getState();
  const context = pageContext(useStore.getState().view, item, pageOff);
  return { context, item: context && item ? item : null };
}

/** The message a question is sent as: the context line first, then the question. */
export function withContext(question: string, context: AskContext | null | undefined): string {
  return context ? `${context.line}\n\n${question}` : question;
}

// The one-time hint under the field: shown until the person removes the chip once or asks
// a first question. One key per browser, which sign-out leaves (it says only that the line
// was seen); storage refused shows it again next time.
export function pageHintSeen(): boolean {
  try {
    return localStorage.getItem(PAGE_HINT_KEY) === "1";
  } catch {
    return false;
  }
}

export function markPageHintSeen(): void {
  try {
    localStorage.setItem(PAGE_HINT_KEY, "1");
  } catch {
    /* storage refused: the hint shows again next time */
  }
}
