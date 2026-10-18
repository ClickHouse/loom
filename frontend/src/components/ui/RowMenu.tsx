import { Dropdown } from "@clickhouse/click-ui/Dropdown";
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
  return (
    <Dropdown modal={false}>
      <MenuTrigger>
        <IconButton
          icon="dots-vertical"
          size="sm"
          label={label}
          tooltip="More actions"
          tabIndex={tabIndex}
          className="relative z-[1]"
        />
      </MenuTrigger>
      {/* Radix names the menu after its trigger; the label names it instead. */}
      <Dropdown.Content align="end" aria-label={label} aria-labelledby={undefined}>
        {items.map((item) => (
          <Dropdown.Item
            key={item.label}
            type={item.danger ? "danger" : "default"}
            disabled={item.disabled}
            onSelect={item.onSelect}
          >
            {/* Not Dropdown.Item's `icon`: Click UI names that svg after the icon, so the
                item's spoken name would carry it. */}
            <span className="inline-flex items-center gap-2">
              {item.icon && <Icon name={item.icon} size="sm" aria-hidden="true" />}
              {item.label}
            </span>
          </Dropdown.Item>
        ))}
      </Dropdown.Content>
    </Dropdown>
  );
}
