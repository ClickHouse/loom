import * as RadixPopover from "@radix-ui/react-popover";
import { Popover } from "@clickhouse/click-ui/Popover";
import type { ComponentProps, ReactElement, ReactNode } from "react";

export interface AnchoredPopoverProps extends Omit<ComponentProps<typeof Popover.Content>, "children"> {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The link the popover sits under; its own onClick opens the popover. */
  anchor: ReactElement;
  children: ReactNode;
}

/**
 * A Click UI Popover opened by a link it is anchored to: a plain click opens it, a
 * modified click follows the link. `Popover.Trigger` (Click UI's, or ui/Menu's) does not
 * fit: Radix's trigger toggles only on a click the link has not prevented, the reverse of
 * this. The anchor is Radix's inside Click UI's Popover, which relies on that Popover
 * being a Radix Popover root (Click UI 0.13): recheck on every Click UI upgrade.
 */
export function AnchoredPopover({ open, onOpenChange, anchor, children, ...content }: AnchoredPopoverProps) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <RadixPopover.Anchor asChild>{anchor}</RadixPopover.Anchor>
      <Popover.Content align="start" className="anchored-popover" {...content}>
        {children}
      </Popover.Content>
    </Popover>
  );
}
