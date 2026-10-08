import type { ReactNode } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";

/**
 * Tip — the one tooltip. Radix, 300 ms delay, the same surface everywhere: text-coloured
 * card, bg-coloured text, an arrow. The child is the trigger (`asChild`), so it must be a
 * focusable element or forward its ref; a tooltip that only opens on hover is not
 * reachable from the keyboard. Never a native `title`: it never shows on touch or to
 * keyboard focus.
 *
 * Carries its own provider so it works anywhere; nesting inside an outer
 * `Tooltip.Provider` is fine.
 */
const TOOLTIP_DELAY_MS = 300;

export const TOOLTIP_CLS = "z-[70] max-w-[320px] px-2 py-1 text-xs rounded-md bg-text text-bg shadow-lg break-words";

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
    <Tooltip.Provider delayDuration={TOOLTIP_DELAY_MS}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content side={side} sideOffset={6} collisionPadding={8} className={TOOLTIP_CLS}>
            {content}
            <Tooltip.Arrow className="fill-text" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}
