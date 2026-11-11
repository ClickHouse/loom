import { createContext, useContext } from "react";
import { KeyButton } from "../../../components/ui/KeyButton";

/** Whether a card's links open what it read: false while the card's conversation is about
 *  another namespace than the header's, where every page and the drawer would show the
 *  header's namespace instead. */
export const CardLinked = createContext(true);

export const useCardLinked = () => useContext(CardLinked);

/** A memory key in a card: a button that opens it in the drawer, or the key as text while
 *  the card's links are off. */
export function CardKey({ memoryKey, onInspect }: { memoryKey: string; onInspect: (key: string) => void }) {
  return useCardLinked() ? (
    <KeyButton memoryKey={memoryKey} onInspect={onInspect} />
  ) : (
    <span className="font-mono text-xs break-all text-text">{memoryKey}</span>
  );
}
