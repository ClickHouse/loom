import { useState } from "react";
import { listTime, listTimeTitle, parseChTime } from "../../lib/format";
import { inRowOrControl } from "./TipTarget";
import { Tip } from "./Tooltip";

export interface ListTimeProps {
  /** An ISO or ClickHouse ("YYYY-MM-DD hh:mm:ss") time. */
  iso: string | null | undefined;
  /** In a list row or a control: no tab stop, the absolute time said. Unset, the time
   *  finds out from where it is drawn, a render later. */
  quiet?: boolean;
  className?: string;
}

/**
 * ListTime — a list row's time: `listTime` ("12m ago", "Sep 20") in a `<time dateTime>`,
 * with the absolute UTC time ("Sep 20, 2026, 14:32 UTC") in a tooltip. In a list or table
 * row, or inside a control, it takes no tab stop and says the absolute time to a screen
 * reader: the row passes `quiet`, else it is measured after mount (`inRowOrControl`).
 * Elsewhere it is focusable, so the tooltip opens from the keyboard. Nothing renders for a
 * missing or unreadable time.
 */
export function ListTime({ iso, quiet: quietProp, className = "" }: ListTimeProps) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  const d = parseChTime(iso);
  if (!d) return null;
  const quiet = quietProp ?? (!!el && inRowOrControl(el));
  return (
    <Tip content={listTimeTitle(iso)}>
      <time
        ref={quietProp === undefined ? setEl : undefined}
        dateTime={d.toISOString()}
        tabIndex={quiet ? undefined : 0}
        className={`tabular whitespace-nowrap ${quiet ? "relative z-[1]" : "rounded"} ${className}`}
      >
        {listTime(iso)}
        {/* Quiet, it takes no focus, so its tooltip is out of a keyboard's reach: the
            absolute time is said instead. */}
        {quiet && <span className="sr-only"> ({listTimeTitle(iso)})</span>}
      </time>
    </Tip>
  );
}
