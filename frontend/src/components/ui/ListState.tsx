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
  /** The rows the filter ran over when they are not the whole list ("the first 50 loaded"). */
  among?: string;
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
 * - `filtered`: an inline row, "No documents match tag:billing" (with `among`, "… in the first
 *   50 loaded"), and a `Clear filters` button that hands focus to the list's search, else
 *   the page's heading.
 * - `error`: `EmptyState` with the warning icon, "Couldn't load <things>", the humanized
 *   message, the request id in muted mono and a secondary `Retry` that hands focus to the
 *   page's heading.
 */
export function ListState(props: ListStateProps) {
  if (props.kind === "filtered") {
    return (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3 text-sm text-text-muted" role="status">
        <span className="min-w-0 break-words">
          No {props.things} match <span className="font-mono text-text">{props.filter}</span>
          {props.among && ` in ${props.among}`}
        </span>
        <Button
          type="empty"
          htmlType="button"
          onClick={() => {
            // Clear filters leaves with this state: focus goes to the list's search field
            // (the toolbar's), else to the page's heading.
            focusInMain(document.querySelector<HTMLElement>("main search input"));
            props.onClear();
          }}
        >
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
        action={{
          label: "Retry",
          type: "secondary",
          // Retry leaves with this state: focus goes to the page's heading, not the page.
          onClick: () => {
            focusInMain(null);
            props.onRetry();
          },
        }}
      >
        {rid && <p className="mt-2 font-mono text-xs text-text-muted">request id {rid}</p>}
      </EmptyState>
    );
  }
  const { icon, title, description, action, details } = props;
  return <EmptyState icon={icon} title={title} description={description} action={action} details={details} />;
}

/** Focus `el`, else the page's heading (made focusable for it). */
function focusInMain(el: HTMLElement | null): void {
  const target = el ?? document.querySelector<HTMLElement>("main h1");
  if (!target) return;
  if (!el && !target.hasAttribute("tabindex")) target.tabIndex = -1;
  target.focus();
}
