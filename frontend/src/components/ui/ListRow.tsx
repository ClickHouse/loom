import { forwardRef, type HTMLAttributes, type ReactNode } from "react";

export interface ListRowProps extends HTMLAttributes<HTMLDivElement> {
  /** A checkbox or icon before the body. */
  leading?: ReactNode;
  /** The row whose detail is open (the inspected memory): Click UI's active stroke. */
  active?: boolean;
  /** Checked for a bulk action: the tint Click UI's Table gives a selected row. */
  checked?: boolean;
  /** A deleted row in a recovery view: a dashed edge. */
  deleted?: boolean;
  children: ReactNode;
}

/**
 * ListRow — one row of a Memories, Library or Conversations list, drawn on Click UI's
 * CardHorizontal tokens (12 × 16 px padding, 4 px radius, its stroke and hover) rather
 * than the component itself: a row holds a checkbox, a stretched open button and chips
 * side by side, and CardHorizontal's body is one block. The open button and the chips are
 * the caller's; the row is only the shell and its states (styles.css `.list-row`).
 */
export const ListRow = forwardRef<HTMLDivElement, ListRowProps>(function ListRow(
  { leading, active, checked, deleted, children, className = "", ...rest },
  ref,
) {
  return (
    <div
      ref={ref}
      {...rest}
      data-active={active || undefined}
      data-checked={checked || undefined}
      data-deleted={deleted || undefined}
      className={`list-row ${className}`}
    >
      {leading && <div className="list-row-leading">{leading}</div>}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
});
