// A card: one of Loom's own reads, made with the person's token when they choose a starter,
// shown in the conversation beside the assistant's answer. Its data comes from the read, not
// from the model's text, so it is exact and costs no model call.
import type { InspectSubject } from "../../../api";

/** The read a card shows; `graph` reads nothing (the card links to the Graph). */
export type CardOp =
  | "namespace.inventory"
  | "namespace.audit"
  | "mentor.brief"
  | "memory.inspect"
  | "knowledge.inspect"
  | "namespace.tune"
  | "graph";

/** The arguments a card's read takes; its starter's question names the first ones. */
export interface CardArgs {
  /** The audit's and the mentor's ledger window. */
  windowSeconds?: number;
  /** The mentor's agent; empty for the namespace's most active writer. */
  agent?: string;
  subject?: InspectSubject;
  id?: string;
  entity?: string;
}

export interface TurnCard {
  op: CardOp;
  args: CardArgs;
  /** When the read landed (ms). */
  readAt?: number;
  /** What it read; absent before it lands, and from a stored copy whose snapshot was too big
   *  to keep. */
  data?: unknown;
  /** Why the read did not run (the person's role or token), or why it failed. */
  error?: string;
  /** `error` is a failed read, not a refusal before it. */
  failed?: boolean;
  /** Why the assistant could not answer when the starter was chosen: the card stands alone. */
  unanswered?: string;
  /** A read is on its way (never stored). */
  busy?: boolean;
}

/** The largest snapshot a stored card keeps (its JSON); past it a reload shows "Read again". */
export const CARD_SNAPSHOT_MAX = 32 * 1024;

/** A card as a conversation stores it: no read in flight, the snapshot only when small. */
export function storedCard(card: TurnCard): TurnCard {
  const { busy: _busy, ...rest } = card;
  if (rest.data === undefined) return rest;
  let size = Number.POSITIVE_INFINITY;
  try {
    size = JSON.stringify(rest.data).length;
  } catch {
    /* not serialisable: dropped */
  }
  if (size <= CARD_SNAPSHOT_MAX) return rest;
  const { data: _data, ...small } = rest;
  return small;
}
