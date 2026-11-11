import type { ReactNode } from "react";
import { Accordion } from "@clickhouse/click-ui/Accordion";
import { Button } from "@clickhouse/click-ui/Button";
import { Icon, type IconName } from "@clickhouse/click-ui/Icon";

interface EmptyStateProps {
  icon: IconName;
  title: string;
  description?: string;
  /** The one action; `secondary` where it is not the surface's primary (an error's Retry). */
  action?: { label: string; onClick: () => void; type?: "primary" | "secondary" };
  /** Controls that own their own state (a button with the dialog it opens), after `action`
   *  in the same row: secondary where `action` is the primary. */
  actions?: ReactNode;
  /** More of how the list fills, in the same plain words, behind a closed "How it works"
   *  disclosure, so the description stays short. */
  details?: ReactNode;
  /** A line under the description (an error's request id). */
  children?: ReactNode;
}

/** Click UI has no empty state; this is Loom's, built from its parts: a muted icon tile,
 *  a product title (600 16 px), muted copy and one primary action. */
export function EmptyState({ icon, title, description, action, actions, details, children }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 px-6">
      <div className="mb-6 grid place-items-center w-16 h-16 rounded-sm bg-surface-2 text-text-muted">
        <Icon name={icon} size="xl" aria-hidden="true" />
      </div>
      <h2 className="text-text">{title}</h2>
      {description && <p className="mt-2 max-w-md text-sm text-text-muted">{description}</p>}
      {children}
      {details && (
        <Accordion title="How it works" size="sm" className="mt-4 max-w-md">
          <div className="mt-2 text-left text-sm text-text-muted">{details}</div>
        </Accordion>
      )}
      {(action || actions) && (
        <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
          {action && (
            <Button type={action.type ?? "primary"} htmlType="button" onClick={action.onClick}>
              {action.label}
            </Button>
          )}
          {actions}
        </div>
      )}
    </div>
  );
}
