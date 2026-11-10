import { Badge } from "@clickhouse/click-ui/Badge";

/** The count over a bell: Click UI's small solid badge, read out by the bell's tooltip. A
 *  count is not a warning: neutral, unless what it counts has failed or is overdue. */
export function CountBadge({ count, state = "neutral" }: { count: number; state?: "neutral" | "danger" }) {
  return (
    <Badge
      text={count > 99 ? "99+" : String(count)}
      type="solid"
      size="sm"
      state={state}
      aria-hidden="true"
      className="absolute -top-2 -right-2 pointer-events-none"
    />
  );
}
