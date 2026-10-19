import type { ReactNode } from "react";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { Icon } from "@clickhouse/click-ui/Icon";

/**
 * MenuChoice — a Click UI Dropdown item that is on or off (`menuitemcheckbox`) or one of a
 * group (`menuitemradio`), for a phone's folded toolbar ("View", "Filters"): a check at the
 * item's end says it is chosen. Not Dropdown.Item's `icon`, which names the svg after the
 * icon in the item's spoken name. Choosing closes the menu, as any item does.
 */
export function MenuChoice({
  checked,
  radio = false,
  onSelect,
  children,
}: {
  checked: boolean;
  radio?: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  return (
    <Dropdown.Item role={radio ? "menuitemradio" : "menuitemcheckbox"} aria-checked={checked} onSelect={onSelect}>
      <span className="flex w-full items-center justify-between gap-3">
        {children}
        <Icon name="check" size="sm" aria-hidden="true" className={checked ? "" : "invisible"} />
      </span>
    </Dropdown.Item>
  );
}

/** A named group of choices in a menu: its label shown above them, and given to the group. */
export function MenuChoiceGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Dropdown.Group aria-label={label}>
      <div aria-hidden="true" className="section-label px-4 pt-2 pb-1">
        {label}
      </div>
      {children}
    </Dropdown.Group>
  );
}
