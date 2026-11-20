import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import type { ReactNode } from "react";

/** A menu item. One a role cannot use (`reason`) stays listed, `aria-disabled` and described
 *  by its reason (`menuReasons`, said once below the items), and choosing it does nothing.
 *  Radix's own `disabled` would drop it from keyboard focus, so its reason would go unread. */
export function GatedItem({
  reason,
  describedBy,
  onSelect,
  danger = false,
  disabled,
  children,
}: {
  reason?: string | null;
  describedBy?: string;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <Dropdown.Item
      type={danger ? "danger" : "default"}
      disabled={reason ? undefined : disabled}
      onSelect={reason ? (e: Event) => e.preventDefault() : onSelect}
      {...(reason ? { "aria-disabled": true, "data-disabled": "", "aria-describedby": describedBy } : {})}
    >
      {children}
    </Dropdown.Item>
  );
}

/** The distinct reasons of a menu's items, once each below them; `idOf` gives each its id,
 *  which an item names as its description. */
export function menuReasons(reasons: (string | null | undefined)[], prefix: string) {
  const list = [...new Set(reasons.filter((r): r is string => !!r))];
  const idOf = (r: string) => `${prefix}-why-${list.indexOf(r)}`;
  const lines = list.map((r) => (
    <p key={r} id={idOf(r)} className="m-0 max-w-72 px-3 py-2 text-xs text-text-muted">
      {r}
    </p>
  ));
  return { idOf, lines };
}
