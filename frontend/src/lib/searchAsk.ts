// Whether the header search offers its answer card, and its placeholder that says so: the
// search also asks wherever the card can be offered (components/HeaderSearch.tsx), before and
// after the search body loads.
import type { AssistantReason } from "./assistantLayout";

export const SEARCH_PLACEHOLDER = "Search profile, memories, library…";
export const ASK_PLACEHOLDER = "Search or ask a question";

/** Whether a known reason keeps the search from offering its answer card. The setup
 *  starting or failing is passing: the card stays, and says a refusal it causes. */
export function blocksCard(reason: AssistantReason | null): boolean {
  return !!reason && reason.kind !== "starting" && reason.kind !== "failing";
}
