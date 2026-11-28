export const ranges = [30, 90, 365];
export const grains = { day: "Daily", week: "Weekly", month: "Monthly" };
export const thresholds = { 0: "Any successful request", 50000: "More than 50,000", 1000000: "More than 1,000,000" };
export const adoptionPaths = { "self-hosted": "/adoption", oss: "/adoption/open-source" };
const defaultGrain = { 30: "day", 90: "week", 365: "month" };
const dayMs = 86_400_000;

export const usageMetricGroups = /** @type {const} */ ([
  {
    title: "Successful requests by feature",
    metrics: [
      ["memory_requests", "Memory"],
      ["code_requests", "Code intelligence"],
      ["knowledge_requests", "Document knowledge"],
      ["conversation_requests", "Conversations"],
      ["tracker_requests", "Issue history"],
      ["task_requests", "Tasks"],
    ],
  },
  {
    title: "Requests by transport",
    metrics: [
      ["mcp_requests", "MCP"],
      ["http_requests", "HTTP"],
    ],
  },
  {
    title: "Model workloads",
    metrics: [
      ["llm_calls", "LLM calls"],
      ["input_tokens", "Input tokens"],
      ["output_tokens", "Output tokens"],
      ["embedding_tokens", "Embedding tokens"],
    ],
  },
  {
    title: "Installation footprint",
    metrics: [
      ["median_namespaces", "Active namespaces"],
      ["median_agents", "Active agents"],
      ["median_sessions", "Active sessions"],
      ["median_active_orgs", "Active organizations"],
      ["median_total_namespaces", "Enabled namespaces"],
    ],
  },
  {
    title: "Request outcomes",
    metrics: [
      ["requests", "All requests"],
      ["server_errors", "Server errors"],
    ],
  },
]);

/** @type {Set<string>} */
export const activityTotalKeys = new Set([
  "operations",
  ...usageMetricGroups.flatMap(({ metrics }) =>
    metrics.map(([key]) => key).filter((key) => !key.startsWith("median_")),
  ),
]);

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
  const requestedDays = Number(params.get("days"));
  const days = ranges.includes(requestedDays) ? requestedDays : 30;
  const grain = params.get("grain");
  const installation = params.get("installation");
  const domain = params.get("domain");

  return {
    days,
    grain: grain === "week" || grain === "month" || (grain === "day" && days === 30) ? grain : defaultGrain[days],
    cohort:
      params.get("cohort") === "week" || params.get("cohort") === "month"
        ? params.get("cohort")
        : days === 30
          ? "week"
          : "month",
    installation: Object.hasOwn(thresholds, installation) ? Number(installation) : 0,
    domain: Object.hasOwn(thresholds, domain) ? Number(domain) : 0,
  };
}

export function lifecycleForRange(rows, filters, today) {
  const cutoff = daysBefore(today, filters.days);
  return [
    { entity: "deployment", threshold: filters.installation, title: "Installations" },
    { entity: "domain", threshold: filters.domain, title: "Domains" },
  ].map((group) => ({
    ...group,
    rows: rows.filter(
      ({ entity, min_requests, grain, bucket, complete_coverage }) =>
        entity === group.entity &&
        min_requests === group.threshold &&
        grain === filters.cohort &&
        bucket >= cutoff &&
        complete_coverage === 1,
    ),
  }));
}

export function relativeChange(current, previous) {
  return previous > 0 ? (current - previous) / previous : null;
}

function openingActivity(days) {
  const totals = Object.fromEntries(
    Array.from(activityTotalKeys, (key) => {
      const values = days.map((row) => row[key]).filter(Number.isFinite);
      return [key, values.length ? values.reduce((total, value) => total + value, 0) : null];
    }),
  );
  const peakActivity = (key, coverage, installations) => {
    const reported = days.filter((row) => Number.isFinite(row[key]));
    const peak = reported.reduce((highest, row) => {
      if (!highest || row[key] > highest[key] || (row[key] === highest[key] && row.period > highest.period)) return row;
      return highest;
    }, null);
    return { [key]: peak?.[key] ?? null, [coverage]: reported.length, [installations]: peak?.[installations] ?? null };
  };
  return {
    ...totals,
    ...peakActivity("active_accounts", "accounts_days_reported", "account_reporting_installations"),
    ...peakActivity("serving_processes", "processes_days_reported", "process_reporting_installations"),
    selected_days_reported: days.length,
  };
}

export function activityForRange(rows, filters, today) {
  const start = daysBefore(today, filters.days);
  return rows
    .filter(({ period, period_end, grain }) => grain === filters.grain && period_end > start && period < today)
    .sort((left, right) => left.period.localeCompare(right.period))
    .map((row) => {
      const current = row.period_end > today;
      const opening = row.period < start;
      if (opening) {
        const daily = rows.filter(
          ({ period, grain }) => grain === "day" && period >= start && period < row.period_end && period < today,
        );
        return { ...row, ...openingActivity(daily), estimate: null, current, opening };
      }
      const elapsed = (Date.parse(today) - Date.parse(row.period)) / dayMs;
      const duration = (Date.parse(row.period_end) - Date.parse(row.period)) / dayMs;
      const estimate =
        current && row.forecast_eligible && elapsed > 0 && row.complete_days_reported === elapsed
          ? Math.round((row.operations / elapsed) * duration)
          : null;
      return { ...row, estimate, current, opening };
    });
}

export function adoptionGrowthForRange(rows, filters, today) {
  const start = daysBefore(today, filters.days);
  return (rows ?? [])
    .filter(({ period, period_end, grain }) => grain === filters.grain && period_end > start && period < today)
    .sort((left, right) => left.period.localeCompare(right.period))
    .map((row) => ({ ...row, current: row.period_end > today }));
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
          "active_installations",
          "successful_requests",
          "projected_requests",
          "days_reported",
          "in_progress",
          "active_accounts",
          "account_days_reported",
          "serving_processes",
          "process_days_reported",
        ],
        ...activity.map((row) => [
          row.period,
          row.period_end,
          row.active,
          row.operations,
          row.estimate,
          row.days_reported,
          row.current,
          row.active_accounts,
          row.accounts_days_reported,
          row.serving_processes,
          row.processes_days_reported,
        ]),
      ];
    case "usage": {
      const keys = [...new Set(usageMetricGroups.flatMap(({ metrics }) => metrics.map(([key]) => key)))];
      return [
        ["period_start_utc", "period_end_utc", "days_reported", "in_progress", ...keys],
        ...activity.map((row) => [
          row.period,
          row.period_end,
          row.days_reported,
          row.current,
          ...keys.map((key) => (Number.isFinite(row[key]) ? row[key] : null)),
        ]),
      ];
    }
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
          rows.map((row) => [
            entity,
            row.bucket,
            row.grain === "week" ? Math.floor((threshold * 7) / 30) : threshold,
            row.new,
            row.retained,
            row.reactivated,
            row.inactive,
          ]),
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
