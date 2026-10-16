import type { ReactNode } from "react";
import { Tip } from "./Tooltip";

/**
 * Non-interactive content with a tooltip — a badge, a time, a shortened label. A tooltip
 * opens on focus as well as hover, so the content is focusable (`role="note"`: it is not an
 * action), and Radix ties the tip to it as its description. `underline` marks inline text
 * as having more to say.
 */
export function TipTarget({
  tip,
  children,
  underline = false,
}: {
  tip: ReactNode;
  children: ReactNode;
  underline?: boolean;
}) {
  return (
    <Tip content={tip}>
      <span
        // biome-ignore lint/a11y/noNoninteractiveTabindex: focusable so keyboard users reach the tooltip; it is not an action
        tabIndex={0}
        role="note"
        className={`inline-flex rounded ${underline ? "underline decoration-dotted" : ""}`}
      >
        {children}
      </span>
    </Tip>
  );
}
