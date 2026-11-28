import { daysBefore } from "./adoption-view.mjs";
import {
  readContainerPulls,
  readGitHubRepository,
  readReleaseDownloads,
  readSource,
  readStarHistory,
} from "./oss-sources.mjs";

const datasets = ["totals", "versions", "retention", "activity", "lifecycle", "oss"];
const grains = ["day", "week", "month"];
const datePattern = /^\d{4}-\d{2}-\d{2}$/;
const versionPattern = /^[0-9][0-9A-Za-z.+-]{0,63}$/;
const smokeVersion = "0.0.0-smoke";
const queryApiOrigin = "https://queries.clickhouse.cloud";
const queryApiPath = /^\/run\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const isCount = (value) => Number.isSafeInteger(value) && value >= 0;
const isDay = (value) => typeof value === "string" && datePattern.test(value) && Number.isFinite(Date.parse(value));
const isVersion = (value) => typeof value === "string" && versionPattern.test(value) && value !== smokeVersion;
const isPeriod = ({ grain, period, period_end }) =>
  grains.includes(grain) && isDay(period) && isDay(period_end) && period < period_end;

function decodeRows(encoded, read) {
  if (!Array.isArray(encoded)) return null;
  try {
    const rows = encoded.map((json) => read(JSON.parse(json)));
    return rows.includes(null) ? null : rows;
  } catch {
    return null;
  }
}

function decodeGrowth(encoded) {
  return (
    decodeRows(encoded, ({ grain, period, period_end, first_active, cumulative_active }) =>
      isPeriod({ grain, period, period_end }) &&
      isCount(first_active) &&
      isCount(cumulative_active) &&
      cumulative_active >= first_active
        ? { grain, period, period_end, first_active, cumulative_active }
        : null,
    )?.sort((left, right) => left.period.localeCompare(right.period)) ?? null
  );
}

function decodeDomainDirectories(encoded) {
  if (!Array.isArray(encoded)) return null;
  try {
    const directories = {};
    for (const json of encoded) {
      const { range_days, domains } = JSON.parse(json);
      if (
        ![30, 90].includes(range_days) ||
        !Array.isArray(domains) ||
        !domains.every((domain) => typeof domain === "string")
      )
        return null;
      directories[range_days] = [...new Set(domains)];
    }
    return directories;
  } catch {
    return null;
  }
}

function decodeDomainAccounts(encoded, directories) {
  if (!directories) return null;
  const rows = decodeRows(encoded, (row) => {
    const {
      range_days,
      domain,
      day,
      active_accounts,
      serving_processes,
      successful_requests,
      active_installations,
      loom_version,
      first_seen,
      last_seen,
    } = row;
    if (![30, 90].includes(range_days) || typeof domain !== "string" || !isDay(day)) return null;
    if (![active_accounts, successful_requests, active_installations].every(isCount)) return null;
    if (serving_processes !== null && !isCount(serving_processes)) return null;
    if (loom_version !== null && !isVersion(loom_version)) return null;
    if (first_seen !== null && (!isDay(first_seen) || first_seen > day)) return null;
    if (last_seen !== null && (!isDay(last_seen) || !first_seen || last_seen < first_seen || last_seen > day))
      return null;
    return {
      range_days,
      domain,
      day,
      active_accounts,
      serving_processes,
      successful_requests,
      active_installations,
      loom_version,
      first_seen,
      last_seen,
    };
  });
  if (!rows) return null;
  return Object.fromEntries(
    [30, 90].map((range) => {
      const allowed = new Set(directories[range]);
      return [
        range,
        Object.fromEntries(
          rows
            .filter(({ range_days, domain }) => range_days === range && allowed.has(domain))
            .map(({ range_days, domain, ...activity }) => [domain, activity]),
        ),
      ];
    }),
  );
}

function decodeFirstObservation(encoded) {
  const rows = decodeRows(encoded, ({ day, loom_version, clickhouse_version }) =>
    isDay(day) && isVersion(loom_version) && isVersion(clickhouse_version)
      ? { day, loom_version, clickhouse_version }
      : null,
  );
  return rows?.[0] ?? null;
}

function decodeVersionTrends(encoded) {
  return (
    decodeRows(encoded, ({ grain, period, period_end, product, version, active_installations }) =>
      isPeriod({ grain, period, period_end }) &&
      ["loom", "clickhouse"].includes(product) &&
      (isVersion(version) || version === smokeVersion) &&
      isCount(active_installations)
        ? { grain, period, period_end, product, version, active_installations }
        : null,
    )?.filter(({ version }) => version !== smokeVersion) ?? null
  );
}

function decodeCompanyTrends(encoded) {
  return decodeRows(encoded, ({ grain, period, period_end, companies, days_reported, complete_days_reported }) =>
    isPeriod({ grain, period, period_end }) &&
    isCount(companies) &&
    (days_reported === undefined || isCount(days_reported)) &&
    (complete_days_reported === undefined ||
      (isCount(complete_days_reported) && complete_days_reported <= days_reported))
      ? {
          grain,
          period,
          period_end,
          companies,
          ...(days_reported !== undefined && { days_reported }),
          ...(complete_days_reported !== undefined && { complete_days_reported }),
        }
      : null,
  );
}

async function loadWarehouse(env) {
  const {
    LOOM_ADOPTION_QUERY_ENDPOINT_URL: address,
    LOOM_ADOPTION_QUERY_KEY_ID: keyId,
    LOOM_ADOPTION_QUERY_KEY_SECRET: keySecret,
  } = env;
  if (!address || !keyId || !keySecret) return {};

  const url = URL.parse(address);
  if (url?.origin !== queryApiOrigin || !queryApiPath.test(url.pathname) || url.username || url.password) {
    console.warn("Adoption dashboard requires a ClickHouse Cloud Query API URL.");
    return {};
  }
  url.search = "";
  url.hash = "";
  url.searchParams.set("format", "JSON");
  const response = await readSource(url, {
    headers: { Authorization: `Basic ${Buffer.from(`${keyId}:${keySecret}`).toString("base64")}` },
    signal: AbortSignal.timeout(30_000),
  });
  const row = response?.data?.[0];
  if (datasets.every((name) => Array.isArray(row?.[name]))) {
    try {
      const aggregates = Object.fromEntries(datasets.map((name) => [name, row[name].map((json) => JSON.parse(json))]));
      for (const total of aggregates.totals) delete total.verified_companies;
      const versions = aggregates.versions.filter(
        ({ loom_version, clickhouse_version, active_installations }) =>
          isVersion(loom_version) && isVersion(clickhouse_version) && isCount(active_installations),
      );
      const domains = Array.isArray(row.domains) ? row.domains.filter((domain) => typeof domain === "string") : null;
      const domainDirectories = decodeDomainDirectories(row.domain_directories);
      const growth = decodeGrowth(row.growth);
      const days = growth?.filter(({ grain }) => grain === "day").map(({ period }) => period);
      const growthCoverage = days?.length ? { first_day: days[0], last_day: days.at(-1) } : null;
      return {
        ...aggregates,
        versions,
        domains,
        domainDirectories,
        domainAccounts: decodeDomainAccounts(row.domain_accounts, domainDirectories),
        firstObservation: decodeFirstObservation(row.first_observation),
        versionTrends: decodeVersionTrends(row.version_trends),
        companyTrends: decodeCompanyTrends(row.company_trends),
        growth,
        growthCoverage,
      };
    } catch {
      console.warn("Adoption Query API returned invalid data.");
      return {};
    }
  }
  console.warn("Adoption Query API is unavailable.");
  return {};
}

const numberOrNull = (value) => (Number.isFinite(value) ? value : null);

function readRegistryMetrics(today) {
  const firstDay = daysBefore(today, 365);
  const historyStart = daysBefore(today, 730);
  const yesterday = daysBefore(today, 1);
  return Promise.all([
    readGitHubRepository(),
    readReleaseDownloads(),
    readSource("https://pypistats.org/api/packages/clickhouse-loom/overall?mirrors=false"),
    readSource(`https://api.npmjs.org/downloads/range/${firstDay}:${yesterday}/@clickhouse/loom`),
    readSource(`https://api.npmjs.org/downloads/range/${historyStart}:${daysBefore(firstDay, 1)}/@clickhouse/loom`),
    readSource("https://crates.io/api/v1/crates/clickhouse-loom/downloads", {
      headers: { "User-Agent": "ClickHouse Loom Adoption (https://github.com/ClickHouse/loom)" },
    }),
    readContainerPulls(),
    readStarHistory(),
  ]);
}

const sundayOf = (day) => daysBefore(day, new Date(day).getUTCDay());

function reconstructStarHistory(weeks, repository, today, historyStart) {
  const { created_at, stargazers_count } = repository ?? {};
  const created = typeof created_at === "string" ? created_at.slice(0, 10) : null;
  if (!weeks?.length || !isDay(created) || !isCount(stargazers_count)) return [];

  let expected = sundayOf(today);
  let remaining = stargazers_count;
  for (const { day, total } of weeks) {
    if (day !== expected) return [];
    remaining -= total;
    expected = daysBefore(expected, 7);
  }
  if (remaining !== 0 || weeks.at(-1).day !== sundayOf(created)) return [];

  let value = 0;
  const history = [];
  for (const { day, days } of weeks.toReversed()) {
    for (const [index, count] of days.entries()) {
      const date = daysBefore(day, -index);
      value += count;
      if (date >= created && date >= historyStart && date < today) {
        history.push({ day: date, value, reconstructed: true });
      }
    }
  }
  return history;
}

async function loadOpenSource(warehouse, today, readRegistries) {
  const historyStart = daysBefore(today, 730);
  const [aggregates, [repository, releaseDownloads, python, npm, olderNpm, rust, pulls, starWeeks]] = await Promise.all(
    [warehouse, readRegistries(today)],
  );
  const oss = aggregates.oss ?? [];
  const series = (source, metric) => oss.filter((row) => row.source === source && row.metric === metric);
  const fetchDays = (rows) =>
    rows
      .filter(({ day, downloads }) => day >= historyStart && day < today && Number.isFinite(downloads))
      .map(({ day, downloads }) => ({ day, value: downloads }))
      .sort((left, right) => left.day.localeCompare(right.day));
  const npmDays = fetchDays([
    ...(Array.isArray(olderNpm?.downloads) ? olderNpm.downloads : []),
    ...(Array.isArray(npm?.downloads) ? npm.downloads : []),
  ]);
  const pythonDays = fetchDays(
    (Array.isArray(python?.data) ? python.data : [])
      .filter(({ category }) => category === "without_mirrors")
      .map(({ date, downloads }) => ({ day: date, downloads })),
  );
  const rustDays = new Map();
  if (Array.isArray(rust?.version_downloads) && Array.isArray(rust.meta?.extra_downloads)) {
    // Recent versions have individual rows. The registry sums all older versions by day.
    for (const row of [...rust.version_downloads, ...rust.meta.extra_downloads]) {
      if (isDay(row?.date) && isCount(row.downloads)) {
        rustDays.set(row.date, (rustDays.get(row.date) ?? 0) + row.downloads);
      }
    }
  }
  const counter = (source, metric, current) => {
    const history = series(source, metric)
      .map(({ day, value }) => ({ day, value }))
      .sort((left, right) => left.day.localeCompare(right.day));
    const value = numberOrNull(current);
    const latest = history.at(-1);
    if (value !== null) {
      if (latest?.day === today) history.pop();
      history.push({ day: today, value });
    }
    return {
      value: value ?? latest?.value ?? null,
      day: value === null ? latest?.day : today,
      history,
    };
  };
  const downloads = (registry, source) => {
    const all = new Map(series(source, "sdk_downloads").map((row) => [row.day, row.value]));
    for (const row of registry) all.set(row.day, row.value);
    return {
      isDownload: true,
      history: [...all]
        .filter(([day]) => day >= historyStart && day < today)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([day, value]) => ({ day, value })),
    };
  };
  const stars = counter("github", "stars", repository?.stargazers_count);
  const firstSnapshot = stars.history[0]?.day ?? today;
  const reconstructedStars = reconstructStarHistory(starWeeks, repository, today, historyStart).filter(
    ({ day }) => day < firstSnapshot,
  );

  return [
    {
      id: "stars",
      label: "GitHub stars",
      source: "GitHub",
      ...stars,
      history: [...stars.history, ...reconstructedStars].sort((left, right) => left.day.localeCompare(right.day)),
    },
    { id: "forks", label: "GitHub forks", source: "GitHub", ...counter("github", "forks", repository?.forks_count) },
    {
      id: "pypi",
      artifact: "clickhouse-loom",
      sdkName: "Python SDK",
      packageUrl: "https://pypi.org/project/clickhouse-loom/",
      label: "Python SDK downloads",
      source: "PyPI",
      ...downloads(pythonDays, "pypi"),
    },
    {
      id: "npm",
      artifact: "@clickhouse/loom",
      sdkName: "TypeScript SDK",
      packageUrl: "https://www.npmjs.com/package/@clickhouse/loom",
      label: "TypeScript SDK downloads",
      source: "npm",
      ...downloads(npmDays, "npm"),
    },
    {
      id: "crates",
      artifact: "clickhouse-loom",
      sdkName: "Rust SDK",
      packageUrl: "https://crates.io/crates/clickhouse-loom",
      label: "Rust SDK downloads",
      source: "crates.io",
      ...downloads(
        Array.from(rustDays, ([day, value]) => ({ day, value })),
        "crates.io",
      ),
    },
    {
      id: "releases",
      label: "Release asset downloads",
      source: "GitHub",
      ...counter("github", "release_asset_downloads_total", releaseDownloads),
    },
    { id: "pulls", label: "Container pulls", source: "GHCR", ...counter("ghcr", "container_pulls_total", pulls) },
  ];
}

export function createAdoptionReader(env, now, readRegistries = readRegistryMetrics) {
  const today = now.toISOString().slice(0, 10);
  const warehouse = loadWarehouse(env);
  const selfHosted = warehouse.then(
    ({
      totals,
      versions,
      retention,
      activity,
      lifecycle,
      domains,
      domainDirectories,
      domainAccounts,
      firstObservation,
      versionTrends,
      companyTrends,
      growth,
      growthCoverage,
    }) => ({
      today,
      warehouseAvailable:
        Array.isArray(totals) &&
        (Array.isArray(domains) || (Array.isArray(domainDirectories?.[30]) && Array.isArray(domainDirectories?.[90]))),
      selfHosted: {
        totals,
        versions,
        retention,
        activity: activity ?? [],
        lifecycle: lifecycle ?? [],
        growth: growth ?? null,
        growthCoverage: growthCoverage ?? null,
        domains: domains ?? null,
        domainDirectories: domainDirectories ?? null,
        domainAccounts: domainAccounts ?? null,
        firstObservation: firstObservation ?? null,
        versionTrends: versionTrends ?? null,
        companyTrends: companyTrends ?? null,
      },
      openSource: [],
    }),
  );
  let withOpenSource;
  return (tab) => {
    if (tab === "self-hosted") return selfHosted;
    withOpenSource ??= Promise.all([selfHosted, loadOpenSource(warehouse, today, readRegistries)]).then(
      ([data, openSource]) => ({
        ...data,
        openSource,
      }),
    );
    return withOpenSource;
  };
}

const warehouseTtlMs = 30_000;
const retryTtlMs = 15_000;
const registryTtlMs = 5 * 60_000;
let registries;
function readPublishedRegistries(today) {
  const now = Date.now();
  if (!registries || now >= registries.expires || registries.today !== today) {
    registries = { today, expires: now + registryTtlMs, data: readRegistryMetrics(today) };
  }
  return registries.data;
}

let published;
export function loadPublishedAdoption(tab) {
  const now = Date.now();
  if (!published || now >= published.expires) {
    const read = createAdoptionReader(process.env, new Date(now), readPublishedRegistries);
    const refresh = { expires: Infinity, read };
    read("self-hosted").then(
      ({ warehouseAvailable }) => {
        refresh.expires = Date.now() + (warehouseAvailable ? warehouseTtlMs : retryTtlMs);
      },
      () => {
        refresh.expires = Date.now() + retryTtlMs;
      },
    );
    published = refresh;
  }
  return published.read(tab);
}
