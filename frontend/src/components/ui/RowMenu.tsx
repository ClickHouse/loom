import { useId } from "react";
import { Dropdown } from "@clickhouse/click-ui/Dropdown";
import { GatedItem, menuReasons } from "./GatedItem";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { IconButton } from "../IconButton";
import { MenuTrigger } from "./Menu";

export interface RowMenuItem {
  label: string;
  icon?: IconName;
  onSelect: () => void;
  /** A destructive action: Click UI's danger item. */
  danger?: boolean;
  disabled?: boolean;
  /** Why the caller's role cannot use it: the item stays listed, `aria-disabled` and
   *  described by this reason (said once below the items), and choosing it does nothing.
   *  Radix's own `disabled` would drop it from keyboard focus, so its reason would go unread. */
  reason?: string | null;
}

export interface RowMenuProps {
  /** The row's name, for the trigger's "Actions for <name>". */
  name: string;
  items: RowMenuItem[];
  /** -1 inside a grid row that is one tab stop (its keys reach the menu). */
  tabIndex?: number;
}

/**
 * RowMenu — a list row's "More" menu: every per-row action but the one
 * dominant inline one, in Click UI's Dropdown behind a `dots-vertical` IconButton named
 * "Actions for <name>". Always visible, never on hover only, at the row's far right after
 * the meta slot; it sits over the row's stretched open button. Keyboard and focus are
 * Radix's: Enter, Space or ArrowDown open it, the arrows move, Escape closes it and focus
 * returns to the trigger. An item that opens a dialog takes the focus there.
 */
export function RowMenu({ name, items, tabIndex }: RowMenuProps) {
  const label = `Actions for ${name}`;
  const prefix = useId();
  const { idOf, lines } = menuReasons(
    items.map((i) => i.reason),
    prefix,
  );
  return (
    <Dropdown modal={false}>
      <MenuTrigger>
        {/* The default 32 px size, as Click UI's Table row actions. */}
        <IconButton
          icon="dots-vertical"
          label={label}
          tooltip="More actions"
          tabIndex={tabIndex}
          className="list-row-control"
        />
      </MenuTrigger>
      {/* Radix names the menu after its trigger; the label names it instead. */}
      <Dropdown.Content align="end" aria-label={label} aria-labelledby={undefined}>
        {items.map((item) => (
          <GatedItem
            key={item.label}
            danger={item.danger}
            disabled={item.disabled}
            reason={item.reason}
            describedBy={item.reason ? idOf(item.reason) : undefined}
            onSelect={item.onSelect}
          >
            {/* Not Dropdown.Item's `icon`: Click UI names that svg after the icon, so the
                item's spoken name would carry it. */}
            <span className="inline-flex items-center gap-2">
              {item.icon && <Icon name={item.icon} size="sm" aria-hidden="true" />}
              {item.label}
            </span>
          </GatedItem>
        ))}
        {lines}
      </Dropdown.Content>
    </Dropdown>
  );
}
