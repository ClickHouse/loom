import { IconButton as ClickIconButton } from "@clickhouse/click-ui/IconButton";
import type { ComponentProps } from "react";
import type { IconButton } from "../IconButton";
import { Unavailable } from "./Unavailable";

/**
 * An icon button like `IconButton` (its name is its label and its tooltip), always rendered
 * through `ui/Unavailable`, so a `reason` that comes and goes never remounts it and a focused
 * button keeps its focus. With a reason it is focusable, `aria-disabled`, its click does
 * nothing and the reason is its tooltip (over the action's name) and description. Apart from
 * IconButton, which the boot chunk holds: only in-page controls a role gates use it.
 */
export function GatedIconButton({
  reason,
  label,
  tooltip,
  className,
  ...rest
}: Omit<ComponentProps<typeof IconButton>, "tooltip"> & { tooltip?: string; reason?: string | null }) {
  // No Radix trigger wraps it (as one wraps IconButton), so `type` is only ever a variant.
  const variant = rest.type ?? "ghost";
  // `gated-icon` gives every variant, danger too, Click UI's icon-button disabled look.
  return (
    <Unavailable reason={reason ?? null} tip={tooltip ?? label}>
      <ClickIconButton
        {...rest}
        type={variant}
        htmlType="button"
        aria-label={label}
        className={className ? `gated-icon ${className}` : "gated-icon"}
        // As IconButton: a tooltip that only repeats the name is not read again as its
        // description; a reason sets its own (ui/Unavailable).
        {...(tooltip ? {} : { "aria-describedby": rest["aria-describedby"] })}
      />
    </Unavailable>
  );
}
