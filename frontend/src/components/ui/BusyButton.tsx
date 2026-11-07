import { forwardRef } from "react";
import { Button, type ButtonProps } from "@clickhouse/click-ui/Button";

/**
 * BusyButton — a Click UI Button that stays on the page while its own read runs (Refresh,
 * Load more, Search). Click UI's `loading` disables the button, which drops keyboard focus
 * to the page mid-read; this one stays focusable, says it is busy (`aria-busy`, dimmed with
 * a progress cursor in styles.css) and ignores a press until the work ends.
 */
export const BusyButton = forwardRef<HTMLButtonElement, ButtonProps & { busy: boolean }>(function BusyButton(
  { busy, onClick, ...props },
  ref,
) {
  return (
    <Button
      {...props}
      ref={ref}
      aria-busy={busy || undefined}
      data-busy-empty={props.type === "empty" || undefined}
      // An `aria-disabled` given from outside (`Unavailable`) holds while the button is idle.
      aria-disabled={busy || props.disabled || props["aria-disabled"] || undefined}
      onClick={(e) => {
        if (busy) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
    />
  );
});
