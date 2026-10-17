import { Icon } from "@clickhouse/click-ui/Icon";

// The direction of the typed edge that reached a node, as `via_direction` gives it: "in"
// draws an arrow pointing left, anything else one pointing right. Text that cannot hold an
// icon (a tooltip, an accessible name) says the word instead.

export function directionWord(direction: string): string {
  return direction === "in" ? "incoming" : "outgoing";
}

/** The arrow inline in a line of text, named for a screen reader. */
export function DirectionIcon({ direction }: { direction: string }) {
  return (
    <span className="inline-flex align-middle mr-0.5">
      <Icon name={direction === "in" ? "arrow-left" : "arrow-right"} size="xs" aria-hidden="true" />
      <span className="sr-only">{directionWord(direction)} </span>
    </span>
  );
}
