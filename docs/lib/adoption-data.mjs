import { daysBefore } from "./adoption-view.mjs";

const database = "loom_product_analytics";
const queries = {
  totals: "public_adoption_totals",
  versions: "public_adoption_versions ORDER BY installations DESC, loom_version, clickhouse_version LIMIT 12",
  retention: "public_adoption_retention ORDER BY age",
  activity: "public_adoption_activity ORDER BY period",
  lifecycle: "public_adoption_lifecycle ORDER BY bucket",
  oss: "public_oss_adoption ORDER BY day, source, metric LIMIT 4000",
};

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
    LOOM_ADOPTION_PUBLIC_CLICKHOUSE_URL: address,
    LOOM_ADOPTION_PUBLIC_CLICKHOUSE_USER: user,
    LOOM_ADOPTION_PUBLIC_CLICKHOUSE_PASSWORD: password,
  } = env;
  if (!address || !user || !password) return {};

  const url = URL.parse(address);
  if (url?.protocol !== "https:" || url.username || url.password) {
    console.warn("Adoption dashboard requires an HTTPS warehouse URL without credentials.");
    return {};
  }
  url.search = "";
  url.searchParams.set("output_format_json_quote_64bit_integers", "0");
  const headers = {
    Authorization: `Basic ${Buffer.from(`${user}:${password}`).toString("base64")}`,
  };
  const results = await Promise.all(
    Object.entries(queries).map(async ([key, statement]) => {
      const response = await readSource(url, {
        method: "POST",
        headers,
        body: `SELECT * FROM ${database}.${statement} FORMAT JSON`,
      });
      if (!Array.isArray(response?.data)) {
        console.warn(`Adoption warehouse data unavailable for ${key}.`);
        return [key, null];
      }
      return [key, response.data];
    }),
  );
  return Object.fromEntries(results);
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

export async function loadAdoption(env = process.env, now = new Date()) {
  const refreshedAt = now.toISOString();
  const today = refreshedAt.slice(0, 10);
  const firstDay = daysBefore(today, 365);
  const historyStart = daysBefore(today, 730);
  const yesterday = daysBefore(today, 1);
  const [warehouse, repository, releases, python, npm, olderNpm, pulls] = await Promise.all([
    loadWarehouse(env),
    readSource("https://api.github.com/repos/ClickHouse/loom"),
    readSource("https://api.github.com/repos/ClickHouse/loom/releases?per_page=100"),
    readSource("https://pypistats.org/api/packages/clickhouse-loom/overall?mirrors=false"),
    readSource(`https://api.npmjs.org/downloads/range/${firstDay}:${yesterday}/@clickhouse/loom`),
    readSource(`https://api.npmjs.org/downloads/range/${historyStart}:${daysBefore(firstDay, 1)}/@clickhouse/loom`),
    containerPulls(),
  ]);

  const oss = warehouse.oss ?? [];
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

  return {
    today,
    refreshedAt,
    selfHosted: {
      totals: warehouse.totals,
      versions: warehouse.versions,
      retention: warehouse.retention,
      activity: warehouse.activity ?? [],
      lifecycle: warehouse.lifecycle ?? [],
    },
    openSource: [
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
    ],
  };
}

let published;
export function loadPublishedAdoption() {
  const now = Date.now();
  if (!published || now >= published.expires) {
    published = { expires: now + 5 * 60_000, data: loadAdoption(process.env, new Date(now)) };
  }
  return published.data;
}
