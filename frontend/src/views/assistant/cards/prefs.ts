// Whether a starter also asks the assistant: the "Also ask the assistant" checkbox, kept in
// this browser for whoever signs in on it (a convenience that holds nothing about the person;
// with storage refused it lasts the tab), and why the assistant cannot answer now when it
// cannot (set by the panel's body).
import { create } from "zustand";

const KEY = "loom_assistant_also_ask";

function readAlsoAsk(): boolean {
  try {
    return localStorage.getItem(KEY) !== "false";
  } catch {
    return true;
  }
}

interface StarterPrefs {
  /** The checkbox as the viewer left it: on unless they turned it off. */
  alsoAsk: boolean;
  /** Why the assistant cannot answer now, or null when it can. */
  cannotAnswer: string | null;
}

export const useStarterPrefs = create<StarterPrefs>(() => ({ alsoAsk: readAlsoAsk(), cannotAnswer: null }));

export function setAlsoAsk(on: boolean): void {
  useStarterPrefs.setState({ alsoAsk: on });
  try {
    localStorage.setItem(KEY, on ? "true" : "false");
  } catch {
    /* storage refused: the choice lasts the tab */
  }
}

export interface AskState {
  /** The starter asks the assistant: the box is on and the assistant can answer. */
  asks: boolean;
  /** Why it cannot, said on its card, when the box is on and the assistant cannot. */
  unanswered: string | null;
}

/** What a starter chosen now does, from the box and why the assistant cannot answer. */
export function askState(alsoAsk: boolean, cannotAnswer: string | null): AskState {
  return { asks: alsoAsk && cannotAnswer === null, unanswered: alsoAsk ? cannotAnswer : null };
}

/** Whether a starter asks, as a selector a component re-renders on (the panel's body keeps
 *  the store's cannotAnswer current). */
export const asks = (s: StarterPrefs): boolean => askState(s.alsoAsk, s.cannotAnswer).asks;

/** The rule at the moment of a click, read from the store. */
export function askNow(): AskState {
  const s = useStarterPrefs.getState();
  return askState(s.alsoAsk, s.cannotAnswer);
}
