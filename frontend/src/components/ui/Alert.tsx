import { Alert as ClickUIAlert, type AlertProps as ClickUIAlertProps } from "@clickhouse/click-ui/Alert";

export interface AlertProps extends ClickUIAlertProps {
  /** `alert` for an error the user must hear now, `status` for news that can wait. */
  role?: "alert" | "status";
}

/**
 * Click UI's Alert, with a `role`. Its root takes one (the component spreads the props it
 * does not know onto it), but its props type declares none, so a live alert needed a
 * wrapping <div>. The spread passes the role past that type.
 */
export function Alert({ role, ...props }: AlertProps) {
  return <ClickUIAlert {...props} {...{ role }} />;
}
