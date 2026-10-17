import type { ReactNode } from "react";
import { Tooltip } from "@clickhouse/click-ui/Tooltip";

/**
 * Tip — the one tooltip: Click UI's Tooltip with its arrow. The child is the trigger
 * (`asChild`), so it must be a focusable element or forward its ref; a tooltip that only
 * opens on hover is not reachable from the keyboard. Never a native `title`: it never
 * shows on touch or to keyboard focus.
 *
 * Under the provider main.tsx mounts (tests get the same one from src/test/rtl.tsx). Its
 * layer comes from styles.css, which lifts every Radix popper over dialogs and toasts.
 */

export interface TipProps {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  /** Keep the tooltip shut, for callers that toggle it. The child stays mounted (and keeps
   *  its focus) either way. */
  disabled?: boolean;
}

export function Tip({ content, children, side = "top", disabled = false }: TipProps) {
  return (
    <Tooltip disabled={disabled}>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Content side={side} collisionPadding={8} maxWidth="320px" showArrow>
        {content}
      </Tooltip.Content>
    </Tooltip>
  );
}
