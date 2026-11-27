import { daysBefore, periodOffset } from "./adoption-view.mjs";

/** @typedef {"day" | "week" | "month"} ChartGrain */
/** @typedef {"additive" | "gauge"} ChartKind */
/** @typedef {{day: string, value: number, days?: number, current?: boolean, partial?: boolean}} ChartPoint */

/** @param {string} day @param {ChartGrain} grain */
export function periodStart(day, grain) {
  if (grain === "month") return `${day.slice(0, 7)}-01`;
  if (grain === "week") return daysBefore(day, (new Date(day).getUTCDay() + 6) % 7);
  return day;
}

/** @param {string} start A calendar period start @param {ChartGrain} grain */
export function periodEnd(start, grain) {
  if (grain === "month") {
    const date = new Date(start);
    date.setUTCMonth(date.getUTCMonth() + 1);
    return date.toISOString().slice(0, 10);
  }
  return daysBefore(start, grain === "week" ? -7 : -1);
}

/** @param {number} value */
export function scaleLimit(value) {
  if (value <= 0) return 0;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  return ([1, 2, 2.5, 5, 10].find((step) => step >= scaled) ?? 10) * magnitude;
}

/**
 * Group dated observations inside [start, end). A gauge uses its last observed level in each period.
 * @param {ReadonlyArray<ChartPoint>} history
 * @param {ChartGrain} grain
 * @param {{start: string, end: string, kind?: ChartKind}} options
 * @returns {ChartPoint[]}
 */
export function groupDailySeries(history, grain, { start, end, kind = "additive" }) {
  const reported = history
    .filter(({ day, value }) => day >= start && day < end && Number.isFinite(value))
    .sort((left, right) => left.day.localeCompare(right.day));
  const periods = Map.groupBy(reported, ({ day }) => periodStart(day, grain));
  return Array.from(periods, ([day, rows]) => {
    const last = rows.at(-1);
    const next = periodEnd(day, grain);
    const value = kind === "additive" ? rows.reduce((total, row) => total + row.value, 0) : last.value;
    const expected = periodOffset(day, next, "day");
    return {
      day,
      value,
      days: rows.length,
      current: next > end,
      partial: kind === "additive" && rows.length < expected,
    };
  });
}

/**
 * Accumulate reported events, or compare levels in adjacent comparable periods.
 * @param {ReadonlyArray<ChartPoint>} rows
 * @param {ChartGrain} grain
 * @param {ChartKind} kind
 * @returns {ChartPoint[]}
 */
export function metricGrowth(rows, grain, kind) {
  const reported = rows.filter(({ value }) => Number.isFinite(value)).sort((a, b) => a.day.localeCompare(b.day));
  if (kind === "additive") {
    let total = 0;
    return reported.map((row) => {
      total += row.value;
      return { ...row, value: total };
    });
  }

  return reported.flatMap((row, index) => {
    const previous = reported[index - 1];
    if (
      !previous ||
      periodOffset(previous.day, row.day, grain) !== 1 ||
      row.current ||
      previous.current ||
      row.partial ||
      previous.partial
    ) {
      return [];
    }
    return [{ ...row, value: row.value - previous.value }];
  });
}

/**
 * @template {string} Metric
 * @param {ReadonlyArray<{period: string, days_reported: number, accounts_days_reported?: number, processes_days_reported?: number, current: boolean} & Partial<Record<Metric, number | null>>>} rows
 * @param {Metric} key
 * @param {ChartGrain} grain
 * @param {"days_reported" | "accounts_days_reported" | "processes_days_reported"} [coverage]
 * @returns {ChartPoint[]}
 */
export function activityPoints(rows, key, grain, coverage = "days_reported") {
  return rows.flatMap((row) => {
    const value = row[key];
    if (typeof value !== "number" || !Number.isFinite(value)) return [];
    const { period: day, current } = row;
    const days = row[coverage];
    return [
      {
        day,
        value,
        days,
        current,
        partial: days === undefined || days < periodOffset(day, periodEnd(day, grain), "day"),
      },
    ];
  });
}
