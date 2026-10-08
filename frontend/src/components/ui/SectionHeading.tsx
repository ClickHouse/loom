import { createElement, type ReactNode } from "react";
import { Info } from "lucide-react";
import { Tip } from "./Tooltip";

/**
 * SectionHeading — one line of uppercase label, an optional count, an
 * optional visible caption, and an optional hint that opens as a tooltip
 * from a small (i) button. Nothing here truncates: the label is `whitespace-nowrap`, the
 * row wraps its caption to a second line when the pane is narrow, and the hint lives in a
 * tooltip instead of trailing the label into an ellipsis ("MEMORY TYPES inspect…").
 *
 * `level` picks the heading element (h2–h4) so the outline of a page is
 * real; the visual is the same at every level. The (i) is 24 px on a fine
 * pointer and 44 px under `pointer: coarse`.
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
        <span className="section-label tabular-nums text-text-subtle" aria-label={`${count} ${label.toLowerCase()}`}>
          {count}
        </span>
      )}
      {caption && <span className="caption whitespace-nowrap">{caption}</span>}
      {hint && (
        <Tip content={hint}>
          <button
            type="button"
            aria-label={`About ${label}`}
            className="inline-flex items-center justify-center min-w-6 min-h-6 -my-1 rounded text-text-subtle hover:text-text pointer-coarse:min-w-11 pointer-coarse:min-h-11 pointer-coarse:-my-2.5"
          >
            <Info size={12} aria-hidden="true" />
          </button>
        </Tip>
      )}
      {actions && <span className="ml-auto inline-flex items-center gap-1 text-2xs">{actions}</span>}
    </div>
  );
}
