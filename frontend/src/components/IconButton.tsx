import { forwardRef, type ReactNode } from "react";
import {
  IconButton as ClickIconButton,
  type IconButtonProps as ClickIconButtonProps,
} from "@clickhouse/click-ui/IconButton";
import { Tip } from "./ui/Tooltip";

/**
 * Icon-only action: Click UI's IconButton with a spoken name and a tooltip. `label` is the
 * `aria-label` and, unless `tooltip` is given, the tooltip text, so a sighted user and a
 * screen reader get the same name.
 */
interface IconButtonProps extends Omit<ClickIconButtonProps, "aria-label" | "title"> {
  label: string;
  /** Longer tooltip copy when the spoken name should stay short. */
  tooltip?: ReactNode;
  /** At least 24 px each way for a mouse (WCAG 2.5.8): Click UI's xs box is 18 px. A coarse
   *  pointer already gets 44 px, as every button does. */
  minTarget?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, tooltip, type, htmlType = "button", minTarget = false, className, ...rest },
  ref,
) {
  // A Radix trigger (`asChild`) hands its child `type="button"`, which on Click UI's
  // IconButton would land on the variant prop.
  const variant = type === undefined || (type as string) === "button" ? "ghost" : type;
  return (
    <Tip content={tooltip ?? label}>
      <ClickIconButton
        ref={ref}
        type={variant}
        htmlType={htmlType}
        aria-label={label}
        className={minTarget ? `pointer-fine:min-h-6 pointer-fine:min-w-6 ${className ?? ""}` : className}
        {...rest}
        // A tooltip that only repeats the name would be read again as its description;
        // a longer one keeps the description Radix ties to it, and a caller's own stays.
        {...(tooltip ? {} : { "aria-describedby": rest["aria-describedby"] })}
      />
    </Tip>
  );
});
