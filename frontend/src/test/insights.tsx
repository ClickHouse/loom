// The assistant's starters and the cards they read, without the panel around them: the
// tests of what each card shows (views/assistant/cards/*.test.tsx). The assistant cannot
// answer by default, so a starter reads its card and asks nothing.
import { useEffect, useRef } from "react";
import { Card } from "../views/assistant/cards/Card";
import { forgetCards } from "../views/assistant/cards/cardStore";
import { useStarterPrefs } from "../views/assistant/cards/prefs";
import { Starters } from "../views/assistant/cards/Starters";
import { resetChat, useChat } from "../views/assistant/chatStore";
import { useStore } from "../lib/state";

/** `all`: "More questions" is opened once on mount, so every starter shows (the tests of a
 *  card reach its starter directly; Starters.test.tsx covers the disclosure). */
export function InsightsHarness({
  cannotAnswer = "model calls are off",
  phone = false,
  all = true,
}: {
  cannotAnswer?: string | null;
  phone?: boolean;
  all?: boolean;
}) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!all) return;
    const more = [...(root.current?.querySelectorAll("button") ?? [])].find((b) => b.textContent === "More questions");
    more?.click();
  }, [all]);
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const turns = useChat((s) => s.turns);
  // The panel's body says why nothing can be asked (views/assistant/Dock.tsx); here it is given.
  useEffect(() => {
    useStarterPrefs.setState({ cannotAnswer });
  }, [cannotAnswer]);
  return (
    <div ref={root}>
      <Starters namespace={namespace} phone={phone} cannotAnswer={cannotAnswer} />
      {turns.map((t) => t.card && <Card key={t.id} turn={t} />)}
    </div>
  );
}

/** A clean conversation and no card read, between tests. */
export function resetInsights(): void {
  forgetCards();
  resetChat(null);
  useStarterPrefs.setState({ alsoAsk: true, cannotAnswer: null });
}
