import { createElement, type ReactNode } from "react";
import { IconButton } from "../IconButton";

/**
 * SectionHeading — one line of section label, an optional count, an
 * optional visible caption, and an optional hint that opens as a tooltip
 * from a small (i) button. Nothing here truncates: the label is `whitespace-nowrap`, the
 * row wraps its caption to a second line when the pane is narrow, and the hint lives in a
 * tooltip instead of trailing the label into an ellipsis ("MEMORY TYPES inspect…").
 *
 * `level` picks the heading element (h2–h4) so the outline of a page is
 * real; the visual is the same at every level. The (i) is Click UI's xs icon button.
 */
export interface SectionHeadingProps {
  label: string;
  /** A count beside the label, tabular. */
  count?: number;
  /** A short visible qualifier after the label, normal case ("in namespace"). */
  caption?: string;
  /** Longer copy, in a tooltip on an (i) button named "About {label}". */
  hint?: string;
  level?: 2 | 3 | 4;
  /** Right-aligned slot for a small action ("clear", "now"). */
  actions?: ReactNode;
  id?: string;
  className?: string;
}

export function SectionHeading({
  label,
  count,
  caption,
  hint,
  level = 3,
  actions,
  id,
  className = "",
}: SectionHeadingProps) {
  return (
    <div className={`flex flex-wrap items-center gap-x-inline gap-y-0.5 min-w-0 mb-1.5 ${className}`}>
      {createElement(`h${level}`, { id, className: "section-label whitespace-nowrap text-text-muted" }, label)}
      {count !== undefined && (
        <span className="section-label tabular-nums text-text-subtle">
          <span aria-hidden="true">{count}</span>
          <span className="sr-only">{`${count} ${label.toLowerCase()}`}</span>
        </span>
      )}
      {caption && <span className="caption whitespace-nowrap">{caption}</span>}
      {hint && <IconButton icon="info-in-circle" size="sm" label={`About ${label}`} tooltip={hint} />}
      {actions && <span className="ml-auto inline-flex items-center gap-1 text-xs">{actions}</span>}
    </div>
  );
}
