import type { ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as RadixPopover from "@radix-ui/react-popover";

/**
 * Triggers for Click UI's `Dropdown` and `Popover`. Click UI's own `Dropdown.Trigger` and
 * `Popover.Trigger` wrap the child in a div that takes `aria-expanded` (not allowed on a
 * div) and gets focus back on Escape instead of the button. These are Radix's triggers on
 * the child itself:
 *
 *   <Dropdown modal={false}><MenuTrigger><Button …/></MenuTrigger><Dropdown.Content>…</Dropdown.Content></Dropdown>
 *
 * `modal={false}`: a modal menu hides the rest of the page with aria-hidden while the
 * skip link and other focusable elements stay in it; focus still moves into the menu and
 * back to the trigger on Escape.
 */
export function MenuTrigger({ children }: { children: ReactNode }) {
  return <DropdownMenu.Trigger asChild>{children}</DropdownMenu.Trigger>;
}

export function PopoverTrigger({ children }: { children: ReactNode }) {
  return <RadixPopover.Trigger asChild>{children}</RadixPopover.Trigger>;
}
