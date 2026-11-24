const database = "loom_product_analytics";
const queries = {
  daily: "public_adoption_daily ORDER BY day LIMIT 30",
  totals: "public_adoption_totals",
  versions: "public_adoption_versions ORDER BY installations DESC, loom_version, clickhouse_version LIMIT 12",
  retention: "public_adoption_retention ORDER BY age",
  activity: "public_adoption_activity ORDER BY period",
  lifecycle: "public_adoption_lifecycle ORDER BY bucket",
  oss: "public_oss_adoption ORDER BY day, source, metric LIMIT 2400",
};

async function fetchJson(url, options) {
  try {
    const response = await fetch(url, {
      ...options,
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    });
    if (response.ok) return await response.json();
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
      const response = await fetchJson(url, {
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

const numberOrNull = (value) => (Number.isFinite(value) ? value : null);

export async function loadAdoption(env = process.env, now = new Date()) {
  const refreshedAt = now.toISOString();
  const today = refreshedAt.slice(0, 10);
  const monthAgo = new Date(Date.parse(today) - 30 * 864e5).toISOString().slice(0, 10);
  const firstMonth = new Date(Date.UTC(Number(today.slice(0, 4)), Number(today.slice(5, 7)) - 12, 1))
    .toISOString()
    .slice(0, 10);
  const yesterday = new Date(Date.parse(today) - 864e5).toISOString().slice(0, 10);
  const [warehouse, repository, releases, python, npm] = await Promise.all([
    loadWarehouse(env),
    fetchJson("https://api.github.com/repos/ClickHouse/loom"),
    fetchJson("https://api.github.com/repos/ClickHouse/loom/releases?per_page=100"),
    fetchJson("https://pypistats.org/api/packages/clickhouse-loom/overall?mirrors=false"),
    fetchJson(`https://api.npmjs.org/downloads/range/${firstMonth}:${yesterday}/@clickhouse/loom`),
  ]);

  const oss = warehouse.oss ?? [];
  const series = (source, metric) => oss.filter((row) => row.source === source && row.metric === metric);
  const fetchDays = (rows) =>
    rows
      .filter(({ day, downloads }) => day >= firstMonth && day < today && Number.isFinite(downloads))
      .map(({ day, downloads }) => ({ day, value: downloads }))
      .sort((left, right) => left.day.localeCompare(right.day));
  const npmDays = fetchDays(Array.isArray(npm?.downloads) ? npm.downloads : []);
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
    const days = [...all]
      .filter(([day]) => day >= firstMonth)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([day, value]) => ({ day, value }));
    const months = Map.groupBy(days, ({ day }) => day.slice(0, 7));
    const monthly = [...months.entries()].map(([month, rows]) => ({
      day: `${month}-01`,
      value: rows.reduce((total, row) => total + row.value, 0),
      days: rows.length,
    }));
    const recent = days.filter(({ day }) => day >= monthAgo);
    return {
      value: recent.length ? recent.reduce((total, row) => total + row.value, 0) : null,
      day: recent.at(-1)?.day,
      days: recent.length,
      history: recent,
      monthly,
    };
  };

  return {
    today,
    refreshedAt,
    selfHosted: {
      totals: warehouse.totals?.[0],
      daily: warehouse.daily,
      versions: warehouse.versions,
      retention: warehouse.retention,
      activity: warehouse.activity ?? [],
      lifecycle: warehouse.lifecycle ?? [],
    },
    openSource: [
      { label: "GitHub stars", source: "GitHub", ...counter("github", "stars", repository?.stargazers_count) },
      { label: "GitHub forks", source: "GitHub", ...counter("github", "forks", repository?.forks_count) },
      { label: "Python SDK downloads", source: "PyPI", ...downloads(pythonDays, "pypi") },
      { label: "TypeScript SDK downloads", source: "npm", ...downloads(npmDays, "npm") },
      {
        label: "Release asset downloads",
        source: "GitHub",
        ...counter("github", "release_asset_downloads_total", releaseDownloads),
      },
      { label: "Container pulls", source: "GHCR", ...counter("ghcr", "container_pulls_total") },
    ],
  };
}
