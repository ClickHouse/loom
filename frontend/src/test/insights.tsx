// The assistant's starters and the cards they read, without the panel around them: the
// tests of what each card shows (views/assistant/cards/*.test.tsx). The assistant cannot
// answer by default, so a starter reads its card and asks nothing.
import { useEffect } from "react";
import { Card } from "../views/assistant/cards/Card";
import { forgetCards } from "../views/assistant/cards/cardStore";
import { useStarterPrefs } from "../views/assistant/cards/prefs";
import { Starters } from "../views/assistant/cards/Starters";
import { resetChat, useChat } from "../views/assistant/chatStore";
import { useStore } from "../lib/state";

export function InsightsHarness({
  cannotAnswer = "model calls are off",
  phone = false,
}: {
  cannotAnswer?: string | null;
  phone?: boolean;
}) {
  const namespace = useStore((s) => s.identity?.namespace ?? "");
  const turns = useChat((s) => s.turns);
  // The panel's body says why nothing can be asked (views/assistant/Dock.tsx); here it is given.
  useEffect(() => {
    useStarterPrefs.setState({ cannotAnswer });
  }, [cannotAnswer]);
  return (
    <div>
      <Starters namespace={namespace} phone={phone} cannotAnswer={cannotAnswer} />
      {turns.map((t) => t.card && <Card key={t.id} turn={t} level={3} />)}
    </div>
  );
}

/** A clean conversation and no card read, between tests. */
export function resetInsights(): void {
  forgetCards();
  resetChat(null);
  useStarterPrefs.setState({ alsoAsk: true, cannotAnswer: null });
}
