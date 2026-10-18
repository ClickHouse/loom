import type { ReactNode } from "react";
import { Icon } from "@clickhouse/click-ui/Icon";
import { TipTarget } from "./TipTarget";
import { Tip } from "./Tooltip";

/**
 * Chip — a filter or value chip. Click UI has no filter chip, so this is drawn as its
 * opaque badge (pill, 1 px stroke, 500 12 px, 2 × 12 px padding) with a 24 px hit target
 * on a fine pointer (44 × 44 px under `pointer: coarse`, a static chip excepted; `target`), an
 * optional tabular count and an optional swatch. One shape for the toggles in Facets, the
 * status filters in Runs, the keys in a run's detail.
 *
 * - With `onClick` it is a `<button>`. Pass `selected` for a toggle: it sets `aria-pressed`
 *   and fills the chip the way Click UI fills a checked control. An action chip (a key that
 *   opens a memory) leaves `selected` out.
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
  /** Drawn outlined on white (a tag), where a plain chip is filled (a filter value). */
  outline?: boolean;
  disabled?: boolean;
  /** Override the accessible name (defaults to label, count and countLabel). */
  ariaLabel?: string;
  tip?: ReactNode;
  /** A small glyph before the label (a lock on a restricted namespace); decorative — the
   *  accessible name carries the meaning. */
  icon?: ReactNode;
  /** Its 44 px target under a coarse pointer: `box` grows the chip; `overlay` keeps its size
   *  and reaches 44 px through a hit area drawn around it (a row's tag, which must not grow
   *  the row); `none` has no target, for a chip a tap passes through. */
  target?: "box" | "overlay" | "none";
  className?: string;
}

const SHAPE = "chip inline-flex items-center justify-center max-w-full min-h-6 border";
/** A chip that takes a press or a focus: a 44 px target under a coarse pointer. */
const BASE = `${SHAPE} pointer-coarse:min-h-11 pointer-coarse:min-w-11`;
const TARGET = { box: BASE, overlay: `${SHAPE} chip-hit`, none: SHAPE };
/** An outlined chip's look, for a chip-shaped control this component cannot draw (a link
 *  that anchors a popover). */
export const CHIP_OUTLINE_CLASS = `${BASE} chip-outline`;

export function Chip({
  label,
  count,
  countLabel,
  selected,
  onClick,
  onRemove,
  swatch,
  mono = false,
  outline = false,
  disabled = false,
  ariaLabel,
  tip,
  icon,
  target = "box",
  className = "",
}: ChipProps) {
  const shape = TARGET[target];
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
        <span className="tabular-nums font-normal shrink-0" aria-hidden="true">
          {count}
        </span>
      )}
    </>
  );
  // A disabled chip takes `.chip-disabled`'s colours (styles.css), which a tone would override.
  const tone = disabled ? "chip-disabled" : selected ? "chip-selected" : outline ? "chip-outline" : "chip-filled";
  const wrap = (el: ReactNode) => (tip ? <Tip content={tip}>{el}</Tip> : el);
  // A span has no role that takes a name, so a static chip's spoken name is sr-only text
  // and the visible text is hidden from assistive technology.
  const spoken = ariaLabel ? (
    <>
      <span className="sr-only">{ariaLabel}</span>
      <span aria-hidden="true" className="contents">
        {inner}
      </span>
    </>
  ) : (
    inner
  );
  // A disabled chip with a tip says why it is disabled; a native `disabled` button takes no
  // hover or focus, so the reason would be unreachable. It stays focusable, aria-disabled.
  const softDisabled = disabled && tip !== undefined;

  if (!onClick && !onRemove) {
    return tip ? (
      <TipTarget tip={tip} className={`${shape} ${tone} ${className}`}>
        {spoken}
      </TipTarget>
    ) : (
      // Nothing to press: no 44 px target on a phone.
      <span className={`${SHAPE} ${tone} ${className}`}>{spoken}</span>
    );
  }
  const main = onClick ? (
    <button
      type="button"
      aria-pressed={selected}
      aria-label={name}
      disabled={disabled && !softDisabled}
      aria-disabled={disabled || undefined}
      onClick={softDisabled ? undefined : onClick}
      className={`${shape} ${tone} ${onRemove ? "rounded-r-none border-r-0" : ""} aria-disabled:cursor-not-allowed ${className}`}
    >
      {inner}
    </button>
  ) : (
    <span className={`${shape} ${tone} rounded-r-none border-r-0 ${className}`}>{spoken}</span>
  );
  if (!onRemove) return wrap(main);
  return (
    <span className="inline-flex max-w-full">
      {wrap(main)}
      <button
        type="button"
        aria-label={`Remove ${label}`}
        disabled={disabled}
        aria-disabled={disabled || undefined}
        onClick={onRemove}
        className={`inline-flex items-center justify-center min-h-6 min-w-6 rounded-r-full border ${disabled ? "chip-disabled" : selected ? "chip-selected" : "chip-filled"} pointer-coarse:min-h-11 pointer-coarse:min-w-11`}
      >
        <Icon name="cross" size="xs" aria-hidden="true" />
      </button>
    </span>
  );
}
