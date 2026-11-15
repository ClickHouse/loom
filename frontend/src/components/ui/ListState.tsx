import { Button } from "@clickhouse/click-ui/Button";
import type { IconName } from "@clickhouse/click-ui/Icon";
import { useEffect, useState, type ReactNode } from "react";
import { errorRequestId } from "../../lib/format";
import { humanizeError } from "../../lib/errorWords";
import { EmptyState } from "../EmptyState";
import { ConnectAgentButton } from "../ConnectAgentButton";

/** Nothing exists yet: the big state, with its one action. */
export interface ListEmptyProps {
  kind: "empty";
  icon: IconName;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
  /** Controls that own their own state, after `action` (`EmptyState`). */
  actions?: ReactNode;
  /** Only an agent fills the list: "Connect an agent" after `action`, which opens the
   *  connect dialog in place. Drawn here, in the chunk every list page loads already, so the
   *  button costs the boot chunk no entry of its own. */
  connect?: "primary" | "secondary";
  details?: ReactNode;
  /** A line under the description. */
  children?: ReactNode;
}

/** Rows exist, but none match the search or filters in force. */
export interface ListFilteredProps {
  kind: "filtered";
  /** The plural noun: "documents", "memories". */
  things: string;
  /** What is in force, as the reader typed it: "tag:billing". Without one (a toggle or a
   *  select, nothing typed), the row says "the filters in force". */
  filter?: string;
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
 * ListState — what a list shows in place of its rows; the toolbar stays
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
  if (props.kind === "filtered") return <FilteredState {...props} />;
  if (props.kind === "error") {
    const message = typeof props.error === "string" ? props.error : humanizeError(props.error, { read: true });
    const id = props.requestId ?? errorRequestId(props.error);
    // An id the message already says (an internal error's) is not printed a second time.
    const rid = id && !message.includes(id) ? id : "";
    // An alert, so a failure that replaces a skeleton or answers a Retry is announced.
    return (
      <div role="alert">
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
          {rid && (
            <p className="mt-2 text-xs text-text-muted">
              request id <code className="font-mono">{rid}</code>
            </p>
          )}
        </EmptyState>
      </div>
    );
  }
  const { icon, title, description, action, actions, connect, details, children } = props;
  return (
    <EmptyState
      icon={icon}
      title={title}
      description={description}
      action={action}
      actions={
        (actions || connect) && (
          <>
            {actions}
            {connect && <ConnectAgentButton type={connect} />}
          </>
        )
      }
      details={details}
    >
      {children}
    </EmptyState>
  );
}

function FilteredState({ things, filter, among, onClear }: ListFilteredProps) {
  // A live region announces a change to its text, not the text it mounts with: it mounts
  // empty and takes its text once it is on the page.
  const [said, setSaid] = useState(false);
  useEffect(() => setSaid(true), []);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3 text-sm text-text-muted">
      <span role="status" className="min-w-0 break-words">
        {said && (
          <>
            No {things} match {filter ? <span className="font-mono text-text">{filter}</span> : "the filters in force"}
            {among && ` in ${among}`}
          </>
        )}
      </span>
      <Button
        type="empty"
        htmlType="button"
        onClick={() => {
          // Clear filters leaves with this state: focus goes to the list's search field
          // (the toolbar's), else to the page's heading.
          focusInMain(document.querySelector<HTMLElement>("main search input"));
          onClear();
        }}
      >
        Clear filters
      </Button>
    </div>
  );
}

/** Focus `el`, else the page's heading (made focusable for it). */
function focusInMain(el: HTMLElement | null): void {
  const target = el ?? document.querySelector<HTMLElement>("main h1");
  if (!target) return;
  if (!el && !target.hasAttribute("tabindex")) target.tabIndex = -1;
  target.focus();
}
