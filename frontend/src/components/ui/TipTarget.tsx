import { forwardRef, type CSSProperties, type ReactNode } from "react";
import { Tip } from "./Tooltip";

/**
 * Non-interactive content with a tooltip — a badge, a time, a shortened label. A tooltip
 * opens on focus as well as hover, so the content is focusable (`role="note"`: it is not an
 * action), and Radix ties the tip to it as its description. `underline` marks inline text
 * as having more to say. `className` replaces the default inline box, for content that
 * draws its own (a chip, a clamped block). `disabled` keeps the same element with no tip,
 * tab stop or role, for content that has more to say only sometimes (a clamped label).
 */
export interface TipTargetProps {
  tip: ReactNode;
  children: ReactNode;
  underline?: boolean;
  className?: string;
  style?: CSSProperties;
  disabled?: boolean;
}

export const TipTarget = forwardRef<HTMLSpanElement, TipTargetProps>(function TipTarget(
  { tip, children, underline = false, className, style, disabled = false },
  ref,
) {
  return (
    <Tip content={tip} disabled={disabled}>
      <span
        ref={ref}
        tabIndex={disabled ? undefined : 0}
        role={disabled ? undefined : "note"}
        className={className ?? `inline-flex rounded ${underline ? "underline decoration-dotted" : ""}`}
        style={style}
      >
        {children}
      </span>
    </Tip>
  );
});

/**
 * Whether a tooltip's content sits in a list row or inside a control (a row's open button):
 * there it takes no tab stop of its own (a row list is one tab stop, and a control cannot
 * hold another), and lifts over the row's stretched button so a hover still reaches it.
 */
export function inRowOrControl(el: Element): boolean {
  return !!el.parentElement?.closest('button, a[href], [role="button"], [role="link"], .list-row');
}
