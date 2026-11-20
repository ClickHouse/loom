import { forwardRef, type ComponentPropsWithoutRef } from "react";
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
// Props and ref pass through, so a Tip can wrap a trigger. A wrapping tooltip's own
// `data-state` is dropped: Radix's trigger spreads its props after its own, and the
// trigger's `data-state` must stay the menu's open state, which styles read.
type WithState = { "data-state"?: string };

export const MenuTrigger = forwardRef<HTMLButtonElement, ComponentPropsWithoutRef<typeof DropdownMenu.Trigger>>(
  function MenuTrigger(props, ref) {
    const { "data-state": _tooltipState, ...rest } = props as typeof props & WithState;
    return <DropdownMenu.Trigger asChild ref={ref} {...rest} />;
  },
);

export const PopoverTrigger = forwardRef<HTMLButtonElement, ComponentPropsWithoutRef<typeof RadixPopover.Trigger>>(
  function PopoverTrigger(props, ref) {
    const { "data-state": _tooltipState, ...rest } = props as typeof props & WithState;
    return <RadixPopover.Trigger asChild ref={ref} {...rest} />;
  },
);
