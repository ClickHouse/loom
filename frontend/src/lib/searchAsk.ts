// Whether the header search offers its answer card, and its placeholder that says so: the
// search also asks wherever the card can be offered (components/HeaderSearch.tsx), before and
// after the search body loads.
import type { MyNamespaceEntry } from "../api";
import { claimReason } from "./assistantClaims";
import { chatOff } from "./assistantDock";
import type { AssistantReason } from "./assistantLayout";
import { isMemoryNamespace } from "./namespace";

export const SEARCH_PLACEHOLDER = "Search profile, memories, library…";
export const ASK_PLACEHOLDER = "Search or ask a question";

/** Whether a known reason keeps the search from offering its answer card. The setup
 *  starting or failing is passing: the card stays, and says a refusal it causes. */
export function blocksCard(reason: AssistantReason | null): boolean {
  return !!reason && reason.kind !== "starting" && reason.kind !== "failing";
}

/** Whether the search can offer Ask AI in this namespace: the chat on, no reason that blocks
 *  the card, no token claim that keeps the assistant from answering, a memory namespace. The
 *  placeholder (layout/Header.tsx) and the search's body (components/HeaderSearch.tsx) both
 *  ask this, so the placeholder does not change when the body loads. */
export function offersAsk(reason: AssistantReason | null, namespace: string, mine: MyNamespaceEntry[] | null): boolean {
  return !chatOff() && !blocksCard(reason) && !claimReason() && isMemoryNamespace(namespace, mine);
}
