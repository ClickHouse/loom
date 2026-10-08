import type { ReactNode } from "react";
import { X } from "lucide-react";
import { Tip } from "./Tooltip";

/**
 * Chip — a filter or value chip. One shape for the toggles in Facets, the status filters
 * in Runs, the keys in a run's detail: `rounded-full`, the border token, a 28 px hit
 * target on a fine pointer (44 × 44 px under `pointer: coarse`), the label at `text-xs`,
 * an optional tabular count, an optional swatch.
 *
 * - With `onClick` it is a `<button aria-pressed>`; `selected` inverts it.
 * - With `onRemove` a second button "Remove {label}" follows (a button
 *   never nests a button).
 * - Without either it is a static `<span>`.
 * - `countLabel` qualifies the count for assistive technology ("in
 *   namespace") without repeating it on every chip visually; the section
 *   heading carries the visible caption.
 * - `tip` shows longer copy on hover and focus (never a native `title`).
 */
export interface ChipProps {
  label: string;
  count?: number;
  countLabel?: string;
  selected?: boolean;
  onClick?: () => void;
  onRemove?: () => void;
  swatch?: string;
  mono?: boolean;
  disabled?: boolean;
  /** Override the accessible name (defaults to label, count and countLabel). */
  ariaLabel?: string;
  tip?: ReactNode;
  /** A small glyph before the label (a lock on a restricted namespace); decorative — the
   *  accessible name carries the meaning. */
  icon?: ReactNode;
  className?: string;
}

const BASE =
  "inline-flex items-center justify-center gap-1 max-w-full min-h-7 px-2 rounded-full border text-xs leading-none pointer-coarse:min-h-11 pointer-coarse:min-w-11 pointer-coarse:px-3";
const OFF = "border-border text-text-muted";
const INTERACTIVE_OFF = "hover:text-text hover:border-border-strong";
const ON = "bg-text text-bg border-transparent";

export function Chip({
  label,
  count,
  countLabel,
  selected = false,
  onClick,
  onRemove,
  swatch,
  mono = false,
  disabled = false,
  ariaLabel,
  tip,
  icon,
  className = "",
}: ChipProps) {
  const name = ariaLabel ?? (count !== undefined ? `${label}, ${count}${countLabel ? ` ${countLabel}` : ""}` : label);
  const inner = (
    <>
      {swatch && (
        <span
          className="inline-block w-2 h-2 rounded-full shrink-0"
          style={{ background: swatch }}
          aria-hidden="true"
        />
      )}
      {icon && (
        <span className="inline-flex shrink-0" aria-hidden="true">
          {icon}
        </span>
      )}
      <span className={`truncate ${mono ? "font-mono" : ""}`}>{label}</span>
      {count !== undefined && (
        <span className="tabular-nums opacity-70 shrink-0" aria-hidden="true">
          {count}
        </span>
      )}
    </>
  );
  const tone = selected ? ON : `${OFF} ${onClick ? INTERACTIVE_OFF : ""}`;
  const wrap = (el: ReactNode) => (tip ? <Tip content={tip}>{el}</Tip> : el);

  if (!onClick && !onRemove) {
    return wrap(
      <span className={`${BASE} ${tone} ${className}`} aria-label={ariaLabel} tabIndex={tip ? 0 : undefined}>
        {inner}
      </span>,
    );
  }
  const main = onClick ? (
    <button
      type="button"
      aria-pressed={selected}
      aria-label={name}
      disabled={disabled}
      onClick={onClick}
      className={`${BASE} ${tone} ${onRemove ? "rounded-r-none border-r-0" : ""} disabled:opacity-50 ${className}`}
    >
      {inner}
    </button>
  ) : (
    <span className={`${BASE} ${tone} rounded-r-none border-r-0 ${className}`} aria-label={ariaLabel}>
      {inner}
    </span>
  );
  if (!onRemove) return wrap(main);
  return (
    <span className="inline-flex max-w-full">
      {wrap(main)}
      <button
        type="button"
        aria-label={`Remove ${label}`}
        disabled={disabled}
        onClick={onRemove}
        className={`inline-flex items-center justify-center min-h-7 min-w-7 rounded-r-full border ${selected ? ON : `${OFF} ${INTERACTIVE_OFF}`} disabled:opacity-50 pointer-coarse:min-h-11 pointer-coarse:min-w-11`}
      >
        <X size={11} aria-hidden="true" />
      </button>
    </span>
  );
}
