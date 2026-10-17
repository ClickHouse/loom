import { useEffect, useState, type ReactNode, type SyntheticEvent } from "react";
import { flushSync } from "react-dom";
import { Slot } from "@radix-ui/react-slot";
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
  // Controlled for its whole life: Click UI's `disabled` alone would switch Radix between
  // controlled and uncontrolled, and Radix would keep an open state from before the switch.
  // While off, the hover and focus that open it never reach Radix: an open Radix cannot show
  // would still make the next tip skip its delay. Each change renders at once, because
  // Radix compares a close with the `open` it last rendered and drops it if that is false.
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  return (
    <Tooltip open={open && !disabled} onOpenChange={(next) => flushSync(() => setOpen(next))}>
      <Tooltip.Trigger asChild>
        <Slot onPointerMove={disabled ? skipRadix : undefined} onFocus={disabled ? skipRadix : undefined}>
          {children}
        </Slot>
      </Tooltip.Trigger>
      <Tooltip.Content side={side} collisionPadding={8} maxWidth="320px" showArrow>
        {content}
      </Tooltip.Content>
    </Tooltip>
  );
}

// Radix's trigger handlers skip an event the child already marked as handled.
function skipRadix(event: SyntheticEvent) {
  event.preventDefault();
}
