import { Button } from "@clickhouse/click-ui/Button";
import type { IconName } from "@clickhouse/click-ui/Icon";
import type { ReactNode } from "react";
import { errorRequestId, humanizeError } from "../../lib/format";
import { EmptyState } from "../EmptyState";

/** Nothing exists yet: the big state, with its one action. */
export interface ListEmptyProps {
  kind: "empty";
  icon: IconName;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  details?: ReactNode;
}

/** Rows exist, but none match the search or filters in force. */
export interface ListFilteredProps {
  kind: "filtered";
  /** The plural noun: "documents", "memories". */
  things: string;
  /** What is in force, as the reader typed or picked it: "tag:billing". */
  filter: string;
  onClear: () => void;
}

/** The read failed. Never an empty state or a zero count. */
export interface ListErrorProps {
  kind: "error";
  things: string;
  /** The failure: an ApiError (humanized, its request id shown), any throw, or a message
   *  a page already humanized. */
  error: unknown;
  /** The request id, when the page kept it apart from a humanized message. */
  requestId?: string;
  onRetry: () => void;
}

export type ListStateProps = ListEmptyProps | ListFilteredProps | ListErrorProps;

/**
 * ListState — what a list shows in place of its rows (SPEC-LISTS C5); the toolbar stays
 * above it in every state, and loading is `ui/LoadingSkeleton` shaped like the rows.
 *
 * - `empty`: `EmptyState`, one action (hide the page header's duplicate primary while it shows).
 * - `filtered`: an inline row, "No documents match tag:billing", and a `Clear filters` button.
 * - `error`: `EmptyState` with the warning icon, "Couldn't load <things>", the humanized
 *   message, the request id in muted mono and a secondary `Retry`.
 */
export function ListState(props: ListStateProps) {
  if (props.kind === "filtered") {
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3 text-sm text-text-muted" role="status">
        <span className="min-w-0 break-words">
          No {props.things} match <span className="font-mono text-text">{props.filter}</span>
        </span>
        <Button type="empty" htmlType="button" onClick={props.onClear}>
          Clear filters
        </Button>
      </div>
    );
  }
  if (props.kind === "error") {
    const message = typeof props.error === "string" ? props.error : humanizeError(props.error);
    const rid = props.requestId ?? errorRequestId(props.error);
    return (
      <EmptyState
        icon="warning"
        title={`Couldn't load ${props.things}`}
        description={message}
        action={{ label: "Retry", onClick: props.onRetry, type: "secondary" }}
      >
        {rid && <p className="mt-2 font-mono text-xs text-text-muted">request id {rid}</p>}
      </EmptyState>
    );
  }
  const { icon, title, description, action, details } = props;
  return <EmptyState icon={icon} title={title} description={description} action={action} details={details} />;
}
