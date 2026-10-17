import type { ReactNode } from "react";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";
import { Tabs } from "@clickhouse/click-ui/Tabs";

/** A Click UI tab with an optional icon inline before its label (Click UI's Icon is a
 *  block, so without the wrapper it stacks above the text). */
export function TabTrigger({ value, icon, children }: { value: string; icon?: IconName; children: ReactNode }) {
  return (
    <Tabs.Trigger value={value}>
      <span className="inline-flex items-center gap-1.5">
        {icon && <Icon name={icon} size="sm" aria-hidden="true" />}
        {children}
      </span>
    </Tabs.Trigger>
  );
}
