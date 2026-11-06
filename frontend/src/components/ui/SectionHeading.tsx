import { Icon } from "@clickhouse/click-ui/Icon";
import { Title } from "@clickhouse/click-ui/Title";
import type { ReactNode } from "react";
import { IconButton } from "../IconButton";

/**
 * SectionHeading — the title of a panel, card or pane section (Click UI's
 * `Title`, 14 px semibold), an optional count, an
 * optional visible caption, and an optional hint that opens as a tooltip
 * from a small (i) button. Nothing here truncates: the label is `whitespace-nowrap`, the
 * row wraps its caption to a second line when the pane is narrow, and the hint lives in a
 * tooltip instead of trailing the label into an ellipsis ("MEMORY TYPES inspect…").
 *
 * `level` picks the heading element (h2–h4) so the outline of a page is
 * real; the visual is the same at every level. The (i) is Click UI's sm icon button, the
 * size Click UI gives its own inline icon actions (a popover's or a file row's close).
 * A label over a group inside a list or menu is `.section-label`, not this.
 *
 * With `fold`, the label is a disclosure button inside the heading (the WAI-ARIA pattern):
 * Click UI's Accordion would name its chevron "accordion icon" in the button's name.
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
  /** Make the label fold the section: `controls` is the id of the body it shows. */
  fold?: { open: boolean; onToggle: () => void; controls: string };
  /** Let a script focus the heading (tabIndex -1), to move the focus to a section's answer. */
  focusable?: boolean;
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
  fold,
  focusable = false,
}: SectionHeadingProps) {
  return (
    <div
      className={`flex flex-wrap items-center gap-x-inline gap-y-0.5 min-w-0 ${fold && !fold.open ? "" : "mb-1.5"} ${className}`}
    >
      <Title type={`h${level}`} size="sm" id={id} className="whitespace-nowrap" tabIndex={focusable ? -1 : undefined}>
        {fold ? (
          <button
            type="button"
            className="inline-flex items-center gap-1 cursor-pointer"
            aria-expanded={fold.open}
            aria-controls={fold.controls}
            onClick={fold.onToggle}
          >
            <Icon name="chevron-right" size="sm" aria-hidden="true" className={fold.open ? "rotate-90" : ""} />
            {label}
          </button>
        ) : (
          label
        )}
      </Title>
      {count !== undefined && (
        <span className="text-sm tabular-nums text-text-muted">
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
