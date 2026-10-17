import { useLayoutEffect, useState, type CSSProperties } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { TipTarget } from "./TipTarget";

/**
 * TextClamp — n lines of text with the full text always reachable. A silent ellipsis on
 * load-bearing text is a defect; this component makes the caller say how the rest is
 * reached:
 *
 * - `reach="tooltip"`: when the text is clamped, it is a `TipTarget`: focusable,
 *   and the full text opens as a tooltip on hover and on focus.
 * - `reach="expand"`: when clamped, a "Show more" button follows; it
 *   toggles the clamp and says so (`aria-expanded`).
 * - `reach="parent"`: the caller's ancestor reaches the text (a row
 *   button that opens the record, itself wrapped in `Tip`); nothing is
 *   added here. Use it only inside an interactive parent.
 *
 * Clamping is measured (`scrollHeight`/`scrollWidth` against the box) and
 * re-measured on resize, so a text that fits carries no tooltip, no
 * tab stop and no button. `max` cuts the displayed characters (the full
 * text still goes to the tooltip) so a 4 KB body never lays out.
 */
export interface TextClampProps {
  text: string;
  lines?: number;
  max?: number;
  reach: "tooltip" | "expand" | "parent";
  mono?: boolean;
  className?: string;
}

function clampStyle(lines: number): CSSProperties {
  if (lines <= 1) return { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
  return {
    display: "-webkit-box",
    WebkitLineClamp: lines,
    WebkitBoxOrient: "vertical",
    overflow: "hidden",
    overflowWrap: "anywhere",
  };
}

/**
 * Whether `el`'s text overflows its box, measured and re-measured as the box resizes or
 * `content` changes. Pass the element through a state ref (`ref={setEl}`), so a remount
 * (a tooltip wrapped round it) measures the new element. With `null` it keeps the last
 * answer.
 */
export function useClamped(el: HTMLElement | null, content: string): boolean {
  const [clamped, setClamped] = useState(false);
  useLayoutEffect(() => {
    if (!el) return;
    const measure = () => setClamped(el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [el, content]);
  return clamped;
}

export function TextClamp({ text, lines = 1, max, reach, mono = false, className = "" }: TextClampProps) {
  const [el, setEl] = useState<HTMLSpanElement | null>(null);
  const [expanded, setExpanded] = useState(false);
  const cut = max !== undefined && text.length > max;
  const shown = cut ? `${text.slice(0, max).trimEnd()}…` : text;
  const overflowing = useClamped(expanded ? null : el, `${lines}:${shown}`);

  const clamped = !expanded && (overflowing || cut);
  const cls = `block ${mono ? "font-mono" : ""} ${className}`;

  if (reach === "expand") {
    return (
      <>
        <span ref={setEl} className={cls} style={expanded ? undefined : clampStyle(lines)}>
          {expanded ? text : shown}
        </span>
        {(clamped || expanded) && (
          <Button
            type="empty"
            htmlType="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((v) => !v)}
            className="mt-0.5"
          >
            {expanded ? "Show less" : "Show more"}
          </Button>
        )}
      </>
    );
  }
  if (reach === "tooltip" && clamped) {
    return (
      <TipTarget ref={setEl} tip={text} className={`${cls} rounded-sm`} style={clampStyle(lines)}>
        {shown}
      </TipTarget>
    );
  }
  return (
    <span ref={setEl} className={cls} style={clampStyle(lines)}>
      {shown}
    </span>
  );
}
