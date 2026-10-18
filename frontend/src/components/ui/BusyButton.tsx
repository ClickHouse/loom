import { Button, type ButtonProps } from "@clickhouse/click-ui/Button";

/**
 * BusyButton — a Click UI Button that stays on the page while its own read runs (Refresh,
 * Load more, Search). Click UI's `loading` disables the button, which drops keyboard focus
 * to the page mid-read; this one stays focusable, says it is busy (`aria-busy`, dimmed with
 * a progress cursor in styles.css) and ignores a press until the work ends.
 */
export function BusyButton({ busy, onClick, ...props }: ButtonProps & { busy: boolean }) {
  return (
    <Button
      {...props}
      aria-busy={busy || undefined}
      aria-disabled={busy || props.disabled || undefined}
      onClick={(e) => {
        if (busy) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
    />
  );
}
