import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Tip } from "./ui/Tooltip";

/**
 * Icon-only action button with a real hit target and a spoken name.
 *
 * A `p-1.5` wrapper around a 14 px glyph is a 26 px target — under the
 * 24 px WCAG floor once the border is counted, and well under what a
 * thumb needs. This renders at least 36 × 36 on a fine pointer and
 * 44 × 44 under `pointer: coarse`, keeps the glyph size the caller
 * passes, and names the control twice: `aria-label` for assistive
 * technology and the shared `Tip` tooltip for sighted users.
 */
const ICON_BTN =
  "inline-flex items-center justify-center min-w-9 min-h-9 pointer-coarse:min-w-11 pointer-coarse:min-h-11 rounded-md transition-colors";

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title" | "aria-label"> {
  /** Accessible name; also the tooltip text unless `tooltip` is given. */
  label: string;
  /** Longer tooltip copy when the spoken name should stay short. */
  tooltip?: ReactNode;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, tooltip, children, className = "", type = "button", ...rest },
  ref,
) {
  return (
    <Tip content={tooltip ?? label}>
      <button ref={ref} type={type} aria-label={label} className={`${ICON_BTN} ${className}`} {...rest}>
        {children}
      </button>
    </Tip>
  );
});
