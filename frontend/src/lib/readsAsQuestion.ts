// The words a question opens with: a question word, an auxiliary, or an ask.
const OPENERS =
  /^(who|what|when|where|why|how|which|whose|is|are|was|were|do|does|did|can|could|should|would|will|explain|summarize|summarise|show me|tell me|list|compare)(\s|$)/i;

/** Whether the header search's text reads as a question for the assistant rather than
 *  keywords: at least three words, ending with "?" or opening with a question word. */
export function readsAsQuestion(q: string): boolean {
  const text = q.trim();
  if (text.split(/\s+/).length < 3) return false;
  return text.endsWith("?") || OPENERS.test(text);
}
