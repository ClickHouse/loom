import type { ReactNode } from "react";
import { Accordion } from "@clickhouse/click-ui/Accordion";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";

interface EmptyStateProps {
  icon: IconName;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  /** Operator detail (op names, worker names, env flags) behind a closed
   *  "How it works" disclosure, so the description itself stays in plain
   * language. */
  details?: ReactNode;
}

/** Click UI has no empty state; this is Loom's, built from its parts: a muted icon tile,
 *  a product title (600 16 px), muted copy and one primary action. */
export function EmptyState({ icon, title, description, action, details }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      <div className="mb-6 grid place-items-center w-16 h-16 rounded-sm bg-surface-2 text-text-subtle">
        <Icon name={icon} size="xl" aria-hidden="true" />
      </div>
      <h2 className="text-lg font-semibold text-text">{title}</h2>
      {description && <p className="mt-2 max-w-md text-sm text-text-muted">{description}</p>}
      {details && (
        <Accordion title="How it works" size="sm" className="mt-4 max-w-md">
          <div className="mt-2 text-left text-sm text-text-muted">{details}</div>
        </Accordion>
      )}
      {action && (
        <Button type="primary" htmlType="button" onClick={action.onClick} className="mt-6">
          {action.label}
        </Button>
      )}
    </div>
  );
}
