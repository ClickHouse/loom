export const ranges = [30, 90, 365];
export const grains = { day: "Daily", week: "Weekly", month: "Monthly" };
export const thresholds = { 0: "Any successful request", 50000: "More than 50,000", 1000000: "More than 1,000,000" };
const defaults = { tab: "self-hosted", days: 30, grain: "week", cohort: "month", installation: 0, domain: 0 };
const dayMs = 86_400_000;

export function daysBefore(day, days) {
  return new Date(Date.parse(day) - days * dayMs).toISOString().slice(0, 10);
}

export function periodOffset(start, day, grain) {
  const monthIndex = (date) => Number(date.slice(0, 4)) * 12 + Number(date.slice(5, 7));
  return grain === "month"
    ? monthIndex(day) - monthIndex(start)
    : Math.round((Date.parse(day) - Date.parse(start)) / (grain === "week" ? 7 * dayMs : dayMs));
}

export function adoptionFilters(params) {
  const days = Number(params.get("days"));
  const grain = params.get("grain");
  const installation = params.get("installation");
  const domain = params.get("domain");

  return {
    tab: params.get("tab") === "oss" ? "oss" : defaults.tab,
    days: ranges.includes(days) ? days : defaults.days,
    grain: Object.hasOwn(grains, grain) ? grain : defaults.grain,
    cohort: params.get("cohort") === "week" ? "week" : defaults.cohort,
    installation: Object.hasOwn(thresholds, installation) ? Number(installation) : defaults.installation,
    domain: Object.hasOwn(thresholds, domain) ? Number(domain) : defaults.domain,
  };
}

export function omitDefaultAdoptionFilters(params) {
  for (const [key, value] of Object.entries(defaults)) {
    if (params.get(key) === String(value)) params.delete(key);
  }
}

export function adoptionUrl(filters, changes = {}, anchor = "") {
  const params = new URLSearchParams({ ...filters, ...changes });
  omitDefaultAdoptionFilters(params);
  const search = params.toString();
  return `/adoption${search ? `?${search}` : ""}${anchor}`;
}

export function relativeChange(current, previous) {
  return previous > 0 ? (current - previous) / previous : null;
}

export function activityForRange(rows, filters, today) {
  const start = daysBefore(today, filters.days);
  return rows
    .filter(({ period, grain }) => period >= start && grain === filters.grain)
    .map((row) => {
      const current = row.period_end > today;
      const elapsed = (Date.parse(today) - Date.parse(row.period)) / dayMs;
      const duration = (Date.parse(row.period_end) - Date.parse(row.period)) / dayMs;
      const estimate =
        current && row.forecast_eligible && elapsed > 0 && row.days_reported === elapsed
          ? Math.round((row.operations / elapsed) * duration)
          : null;
      return { ...row, estimate, current };
    });
}

export function summarizeOss(metrics, range, today) {
  const start = daysBefore(today, range);
  const priorStart = daysBefore(today, range * 2);
  return metrics.map((metric) => {
    const history = metric.history.filter(({ day }) => day >= start);
    if (!metric.isDownload) {
      const baseline = metric.history.find(({ day }) => day === start);
      const change = baseline && metric.value !== null ? metric.value - baseline.value : null;
      return { ...metric, history, change };
    }

    const previous = metric.history.filter(({ day }) => day >= priorStart && day < start);
    const count = (rows) => rows.reduce((total, row) => total + row.value, 0);
    const monthly = Array.from(
      Map.groupBy(history, ({ day }) => day.slice(0, 7)),
      ([month, rows]) => ({
        day: `${month}-01`,
        value: count(rows),
        days: rows.length,
      }),
    );
    return {
      ...metric,
      history,
      monthly,
      value: history.length ? count(history) : null,
      day: history.at(-1)?.day,
      days: history.length,
      change:
        history.length === range && previous.length === range ? relativeChange(count(history), count(previous)) : null,
    };
  });
}

export function adoptionCsv(dataset, activity, lifecycle, oss) {
  switch (dataset) {
    case "activity":
      return [
        [
          "period_start_utc",
          "period_end_utc",
          "reporting_installations",
          "active_installations",
          "successful_requests",
          "projected_requests",
          "days_reported",
          "in_progress",
        ],
        ...activity.map((row) => [
          row.period,
          row.period_end,
          row.reporting,
          row.active,
          row.operations,
          row.estimate,
          row.days_reported,
          row.current,
        ]),
      ];
    case "lifecycle":
      return [
        [
          "entity",
          "period_start_utc",
          "minimum_requests_exclusive",
          "new",
          "retained",
          "reactivated",
          "became_inactive",
        ],
        ...lifecycle.flatMap(({ entity, threshold, rows }) =>
          rows.map((row) => [entity, row.bucket, threshold, row.new, row.retained, row.reactivated, row.inactive]),
        ),
      ];
    case "downloads":
      return [
        ["registry", "package", "day_utc", "downloads"],
        ...oss
          .filter(({ isDownload }) => isDownload)
          .flatMap((metric) => metric.history.map(({ day, value }) => [metric.source, metric.artifact, day, value])),
      ];
    case "stars":
      return [
        ["day_utc", "github_stars"],
        ...oss.find(({ id }) => id === "stars").history.map(({ day, value }) => [day, value]),
      ];
    default:
      return null;
  }
}

export function csvBody(rows) {
  return (
    rows
      .map((row) =>
        row
          .map((value) => {
            const raw = String(value ?? "");
            const text = typeof value === "string" && /^[=+\-@\t\r\n]/.test(raw) ? "'" + raw : raw;
            return '"' + text.replaceAll('"', '""') + '"';
          })
          .join(","),
      )
      .join("\r\n") + "\r\n"
  );
}
