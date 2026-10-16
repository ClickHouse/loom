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
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, tooltip, type = "ghost", htmlType = "button", ...rest },
  ref,
) {
  return (
    <Tip content={tooltip ?? label}>
      <ClickIconButton ref={ref} type={type} htmlType={htmlType} aria-label={label} {...rest} />
    </Tip>
  );
});
