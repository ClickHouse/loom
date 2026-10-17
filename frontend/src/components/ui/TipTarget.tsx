import { forwardRef, type CSSProperties, type ReactNode } from "react";
import { Tip } from "./Tooltip";

/**
 * Non-interactive content with a tooltip — a badge, a time, a shortened label. A tooltip
 * opens on focus as well as hover, so the content is focusable (`role="note"`: it is not an
 * action), and Radix ties the tip to it as its description. `underline` marks inline text
 * as having more to say. `className` replaces the default inline box, for content that
 * draws its own (a chip, a clamped block).
 */
export interface TipTargetProps {
  tip: ReactNode;
  children: ReactNode;
  underline?: boolean;
  className?: string;
  style?: CSSProperties;
}

export const TipTarget = forwardRef<HTMLSpanElement, TipTargetProps>(function TipTarget(
  { tip, children, underline = false, className, style },
  ref,
) {
  return (
    <Tip content={tip}>
      <span
        ref={ref}
        // biome-ignore lint/a11y/noNoninteractiveTabindex: focusable so keyboard users reach the tooltip; it is not an action
        tabIndex={0}
        role="note"
        className={className ?? `inline-flex rounded ${underline ? "underline decoration-dotted" : ""}`}
        style={style}
      >
        {children}
      </span>
    </Tip>
  );
});
