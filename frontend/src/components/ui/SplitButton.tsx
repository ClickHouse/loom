import { type ComponentProps, useLayoutEffect, useRef } from "react";
import { SplitButton as ClickSplitButton } from "@clickhouse/click-ui/SplitButton";

export type SplitButtonProps = ComponentProps<typeof ClickSplitButton> & {
  /** The menu half's name ("More actions for acme/loom"). */
  menuLabel: string;
};

/**
 * SplitButton — Click UI's, secondary by default. Click UI 0.13 draws the menu half as a
 * `div` with no role, no tab stop and a chevron named "chevron-down", so a keyboard cannot
 * reach the menu and a screen reader hears nothing useful. This names it, makes it a
 * focusable button (Radix's trigger already opens on Enter, Space and ArrowDown) and hides
 * the icon. Delete once Click UI draws a button.
 */
export function SplitButton({ menuLabel, type = "secondary", modal = false, ...rest }: SplitButtonProps) {
  const box = useRef<HTMLSpanElement>(null);
  const disabled = !!rest.disabled;
  useLayoutEffect(() => {
    const trigger = box.current?.querySelector<HTMLElement>('[data-testid="split-button-dropdown"]');
    if (!trigger) return;
    trigger.setAttribute("role", "button");
    trigger.setAttribute("aria-label", menuLabel);
    trigger.tabIndex = disabled ? -1 : 0;
    if (disabled) trigger.setAttribute("aria-disabled", "true");
    else trigger.removeAttribute("aria-disabled");
    for (const svg of trigger.querySelectorAll("svg")) {
      svg.setAttribute("aria-hidden", "true");
      svg.removeAttribute("role");
      svg.removeAttribute("aria-label");
    }
  });
  return (
    <span ref={box} className="split-button contents">
      <ClickSplitButton type={type} modal={modal} {...rest} />
    </span>
  );
}
