import type { ReactNode } from "react";
import { BigStat } from "@clickhouse/click-ui/BigStat";

/**
 * Stat — one figure in a row of them: Click UI's small BigStat (a 14 px label over a 16 px
 * semibold value, on its padding, stroke and radius) with an optional hint under the
 * value, which BigStat has no slot for. `error` is BigStat's danger stroke and label, for
 * a figure that is a problem.
 */
export interface StatProps {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  error?: boolean;
}

export function Stat({ label, value, hint, error = false }: StatProps) {
  return (
    <BigStat
      size="sm"
      fillWidth
      error={error}
      label={label}
      title={
        <>
          <span className="tabular">{value}</span>
          {hint && <span className="block text-xs font-normal text-text-muted">{hint}</span>}
        </>
      }
    />
  );
}
