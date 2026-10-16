import type { ReactNode } from "react";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { Tooltip } from "@clickhouse/click-ui/Tooltip";

/**
 * Tip — the one tooltip: Click UI's Tooltip with its arrow. The child is the trigger
 * (`asChild`), so it must be a focusable element or forward its ref; a tooltip that only
 * opens on hover is not reachable from the keyboard. Never a native `title`: it never
 * shows on touch or to keyboard focus.
 *
 * Carries its own provider so it works outside the app shell (tests, portals).
 */
const TOOLTIP_DELAY_MS = 300;
/** Above dialogs (z-50/60) and toasts (z-[65]); styles.css lifts every Radix popper here. */
export const POPOVER_LAYER = "z-[70]";

export interface TipProps {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  /** Render the child alone (no tooltip) — for callers that toggle it. */
  disabled?: boolean;
}

export function Tip({ content, children, side = "top", disabled = false }: TipProps) {
  if (disabled) return <>{children}</>;
  return (
    <RadixTooltip.Provider delayDuration={TOOLTIP_DELAY_MS}>
      <Tooltip>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Content side={side} collisionPadding={8} maxWidth="320px" className={POPOVER_LAYER} showArrow>
          {content}
        </Tooltip.Content>
      </Tooltip>
    </RadixTooltip.Provider>
  );
}
