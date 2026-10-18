import { Button, type ButtonProps } from "@clickhouse/click-ui/Button";

/**
 * BusyButton — a Click UI Button whose own action is in flight. Click UI's `loading`
 * disables the button, which drops keyboard focus to the page; this one stays focusable,
 * says it is busy (`aria-busy`, `aria-disabled`) and ignores a press until the work ends.
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
