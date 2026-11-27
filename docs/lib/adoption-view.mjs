export const ranges = [30, 90, 365];
export const grains = { day: "Daily", week: "Weekly", month: "Monthly" };
export const thresholds = { 0: "Any successful request", 50000: "More than 50,000", 1000000: "More than 1,000,000" };
const defaults = { tab: "self-hosted", days: 30, grain: "week", cohort: "week", installation: 0, domain: 0 };
const dayMs = 86_400_000;
const personalEmailDomains = new Set([
  "126.com",
  "163.com",
  "aol.com",
  "fastmail.com",
  "gmail.com",
  "gmx.com",
  "gmx.de",
  "gmx.net",
  "googlemail.com",
  "hey.com",
  "hotmail.com",
  "icloud.com",
  "live.com",
  "mac.com",
  "magenta.de",
  "mail.com",
  "mail.ru",
  "me.com",
  "msn.com",
  "myyahoo.com",
  "outlook.com",
  "pm.me",
  "proton.me",
  "protonmail.ch",
  "protonmail.com",
  "qq.com",
  "rocketmail.com",
  "t-online.de",
  "web.de",
  "yahoo.co.jp",
  "yahoo.co.uk",
  "yahoo.com",
  "yahoo.fr",
  "yandex.com",
  "yandex.ru",
  "ymail.com",
]);

export function adoptionDomainsForRange({ domains, domainDirectories }, days) {
  const directoryDays = Math.min(days, 90);
  const rankedDomains = domainDirectories?.[directoryDays];
  const reportedDomains = rankedDomains ?? (directoryDays === 90 ? domains : null);
  const companyDomains = reportedDomains
    ?.map((domain) => domain.toLowerCase())
    .filter((domain) => !personalEmailDomains.has(domain));
  return {
    days: directoryDays,
    domains: reportedDomains,
    ranked: rankedDomains != null,
    companyCount: companyDomains ? new Set(companyDomains).size : null,
  };
}

export const usageMetricGroups = /** @type {const} */ ([
  {
    title: "Successful requests by feature",
    note: "All charts use the same request scale.",
    sharedScale: true,
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
    note: "Successful requests over MCP and HTTP use the same scale.",
    sharedScale: true,
    metrics: [
      ["mcp_requests", "MCP"],
      ["http_requests", "HTTP"],
    ],
  },
  {
    title: "Model workloads",
    note: "Each chart has its own scale.",
    sharedScale: false,
    metrics: [
      ["llm_calls", "LLM calls"],
      ["input_tokens", "Input tokens"],
      ["output_tokens", "Output tokens"],
      ["embedding_tokens", "Embedding tokens"],
    ],
  },
  {
    title: "Installation footprint",
    note: "Median daily peak among active installations. Each chart has its own scale.",
    sharedScale: false,
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
    note: "Each chart has its own scale.",
    sharedScale: false,
    metrics: [
      ["requests", "All requests"],
      ["server_errors", "Server errors"],
    ],
  },
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
  const days = Number(params.get("days"));
  const grain = params.get("grain");
  const installation = params.get("installation");
  const domain = params.get("domain");

  return {
    tab: params.get("tab") === "oss" ? "oss" : defaults.tab,
    days: ranges.includes(days) ? days : defaults.days,
    grain: Object.hasOwn(grains, grain) ? grain : defaults.grain,
    cohort: params.get("cohort") === "month" ? "month" : defaults.cohort,
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
  if (params.get("tab") === "oss") {
    for (const key of ["grain", "cohort", "installation", "domain"]) params.delete(key);
  }
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
    .sort((left, right) => left.period.localeCompare(right.period))
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
