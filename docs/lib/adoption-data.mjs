import { daysBefore } from "./adoption-view.mjs";

const datasets = ["totals", "versions", "retention", "activity", "lifecycle", "oss"];
const queryApiOrigin = "https://queries.clickhouse.cloud";
const queryApiPath = /^\/run\/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

async function readSource(url, options, decode = (response) => response.json()) {
  try {
    const response = await fetch(url, {
      ...options,
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    });
    if (response.ok) return await decode(response);
  } catch {
    // A network or response error leaves the source unavailable for this refresh.
  }
  return null;
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
  });
  const row = response?.data?.[0];
  if (datasets.every((name) => Array.isArray(row?.[name]))) {
    try {
      const aggregates = Object.fromEntries(datasets.map((name) => [name, row[name].map((json) => JSON.parse(json))]));
      const domains = Array.isArray(row.domains) ? row.domains.filter((domain) => typeof domain === "string") : null;
      return { ...aggregates, domains };
    } catch {
      console.warn("Adoption Query API returned invalid data.");
      return {};
    }
  }
  console.warn("Adoption Query API is unavailable.");
  return {};
}

async function containerPulls() {
  return await readSource("https://github.com/ClickHouse/loom/pkgs/container/loom", undefined, async (response) => {
    // GitHub displays a compact count with the exact total in the heading title.
    const html = await response.text();
    const total = html.match(/<span[^>]*>\s*Total downloads\s*<\/span>\s*<h3\s+title="([0-9]+)"/)?.[1];
    const count = Number(total);
    return Number.isSafeInteger(count) ? count : null;
  });
}

const numberOrNull = (value) => (Number.isFinite(value) ? value : null);

async function loadOpenSource(warehouse, today) {
  const firstDay = daysBefore(today, 365);
  const historyStart = daysBefore(today, 730);
  const yesterday = daysBefore(today, 1);
  const [aggregates, repository, releases, python, npm, olderNpm, pulls] = await Promise.all([
    warehouse,
    readSource("https://api.github.com/repos/ClickHouse/loom"),
    readSource("https://api.github.com/repos/ClickHouse/loom/releases?per_page=100"),
    readSource("https://pypistats.org/api/packages/clickhouse-loom/overall?mirrors=false"),
    readSource(`https://api.npmjs.org/downloads/range/${firstDay}:${yesterday}/@clickhouse/loom`),
    readSource(`https://api.npmjs.org/downloads/range/${historyStart}:${daysBefore(firstDay, 1)}/@clickhouse/loom`),
    containerPulls(),
  ]);

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
  const releaseDownloads =
    Array.isArray(releases) && releases.length < 100
      ? releases.flatMap(({ assets }) => assets ?? []).reduce((total, asset) => total + asset.download_count, 0)
      : null;
  const counter = (source, metric, current) => {
    const history = series(source, metric).map(({ day, value }) => ({ day, value }));
    const value = numberOrNull(current);
    const latest = history.at(-1);
    if (value !== null && history.length) history.push({ day: today, value });
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

  return [
    {
      id: "stars",
      label: "GitHub stars",
      source: "GitHub",
      ...counter("github", "stars", repository?.stargazers_count),
    },
    { id: "forks", label: "GitHub forks", source: "GitHub", ...counter("github", "forks", repository?.forks_count) },
    {
      id: "pypi",
      artifact: "clickhouse-loom",
      label: "Python SDK downloads",
      source: "PyPI",
      ...downloads(pythonDays, "pypi"),
    },
    {
      id: "npm",
      artifact: "@clickhouse/loom",
      label: "TypeScript SDK downloads",
      source: "npm",
      ...downloads(npmDays, "npm"),
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

export function createAdoptionReader(env, now) {
  const refreshedAt = now.toISOString();
  const today = refreshedAt.slice(0, 10);
  const warehouse = loadWarehouse(env);
  const selfHosted = warehouse.then(({ totals, versions, retention, activity, lifecycle, domains }) => ({
    today,
    refreshedAt,
    selfHosted: {
      totals,
      versions,
      retention,
      activity: activity ?? [],
      lifecycle: lifecycle ?? [],
      domains: domains ?? null,
    },
    openSource: [],
  }));
  let withOpenSource;
  return (tab) => {
    if (tab === "self-hosted") return selfHosted;
    withOpenSource ??= Promise.all([selfHosted, loadOpenSource(warehouse, today)]).then(([data, openSource]) => ({
      ...data,
      openSource,
    }));
    return withOpenSource;
  };
}

let published;
export function loadPublishedAdoption(tab) {
  const now = Date.now();
  if (!published || now >= published.expires) {
    published = { expires: now + 5 * 60_000, read: createAdoptionReader(process.env, new Date(now)) };
  }
  return published.read(tab);
}
