import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { Button } from "@clickhouse/click-ui/Button";
import { Tip } from "./Tooltip";

/**
 * TextClamp — n lines of text with the full text always reachable. A silent ellipsis on
 * load-bearing text is a defect; this component makes the caller say how the rest is
 * reached:
 *
 * - `reach="tooltip"`: when the text is clamped, the span is focusable and
 *   the full text opens as a tooltip on hover and on focus.
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

export function TextClamp({ text, lines = 1, max, reach, mono = false, className = "" }: TextClampProps) {
  const ref = useRef<HTMLSpanElement>(null);
  const [expanded, setExpanded] = useState(false);
  const cut = max !== undefined && text.length > max;
  const shown = cut ? `${text.slice(0, max).trimEnd()}…` : text;
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const measure = () => setOverflowing(el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [shown, lines, expanded]);

  const clamped = !expanded && (overflowing || cut);
  const cls = `block ${mono ? "font-mono" : ""} ${className}`;

  if (reach === "expand") {
    return (
      <>
        <span ref={ref} className={cls} style={expanded ? undefined : clampStyle(lines)}>
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
  const span = (
    <span
      ref={ref}
      className={`${cls} ${clamped && reach === "tooltip" ? "outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm" : ""}`}
      style={clampStyle(lines)}
      tabIndex={clamped && reach === "tooltip" ? 0 : undefined}
    >
      {shown}
    </span>
  );
  if (reach === "tooltip" && clamped) return <Tip content={text}>{span}</Tip>;
  return span;
}
