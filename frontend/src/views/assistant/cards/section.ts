import { createContext, useContext } from "react";

/** Where a card body's sections sit: the card's heading id, which their ids start with (two
 *  cards of one op in a conversation keep distinct ids), and their heading level, one under
 *  the card's own heading. */
export const CardSection = createContext<{ prefix: string; level: 3 | 4 }>({ prefix: "card", level: 3 });

export const useCardSection = () => useContext(CardSection);
