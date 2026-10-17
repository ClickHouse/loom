import { useState } from "react";
import { listTime, listTimeTitle, parseChTime } from "../../lib/format";
import { inRowOrControl } from "./TipTarget";
import { Tip } from "./Tooltip";

export interface ListTimeProps {
  /** An ISO or ClickHouse ("YYYY-MM-DD hh:mm:ss") time. */
  iso: string | null | undefined;
  className?: string;
}

/**
 * ListTime — a list row's time: `listTime` ("12m ago", "Sep 20") in a `<time dateTime>`,
 * with the absolute UTC time ("Sep 20, 2026, 14:32 UTC") in a tooltip. In a list row or
 * inside a control it takes no tab stop (`inRowOrControl`); elsewhere it is focusable, so
 * the tooltip opens from the keyboard. Nothing renders for a missing or unreadable time.
 */
export function ListTime({ iso, className = "" }: ListTimeProps) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const d = parseChTime(iso);
  if (!d) return null;
  const quiet = !!el && inRowOrControl(el);
  return (
    <Tip content={listTimeTitle(iso)}>
      <time
        ref={setEl}
        dateTime={d.toISOString()}
        tabIndex={quiet ? undefined : 0}
        className={`tabular whitespace-nowrap ${quiet ? "relative z-[1]" : "rounded"} ${className}`}
      >
        {listTime(iso)}
      </time>
    </Tip>
  );
}
