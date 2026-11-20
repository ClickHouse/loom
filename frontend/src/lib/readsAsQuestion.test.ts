import { describe, expect, it } from "vitest";
import { readsAsQuestion } from "./readsAsQuestion";

describe("readsAsQuestion", () => {
  it.each([
    // Three words or more, ending with "?" or opening with a question word or an ask.
    ["what does billing remember", true],
    ["Who wrote the invoice policy", true],
    ["  how are refunds handled  ", true],
    ["refunds for enterprise accounts?", true],
    ["is billing weekly", true],
    ["are the invoices sent", true],
    ["was the job moved", true],
    ["were reminders sent", true],
    ["do we retry charges", true],
    ["does billing run weekly", true],
    ["did the import finish", true],
    ["can agents read this", true],
    ["could this be stale", true],
    ["should I pin this", true],
    ["would a tag help", true],
    ["will the job run", true],
    ["which agent wrote it", true],
    ["whose memory is this", true],
    ["when did it change", true],
    ["where are the specs", true],
    ["why is recall low", true],
    ["explain the billing schedule", true],
    ["Summarize this namespace please", true],
    ["summarise this namespace please", true],
    ["show me recent changes", true],
    ["Tell me about billing", true],
    ["list the active agents", true],
    ["compare these two documents", true],
    // Fewer than three words, even with a question mark or a question word.
    ["billing?", false],
    ["what billing", false],
    ["show me", false],
    // Keywords and filters stay results-first.
    ["billing invoices weekly", false],
    ["tag:work invoices billing", false],
    ["whatever is in billing", false],
    ["showme the money now", false],
    ["listing the agents now", false],
    ["", false],
    ["   ", false],
  ])("%j reads as a question: %s", (q, expected) => {
    expect(readsAsQuestion(q)).toBe(expected);
  });
});
