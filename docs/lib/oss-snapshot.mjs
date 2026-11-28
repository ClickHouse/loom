import { timingSafeEqual } from "node:crypto";
import { readContainerPulls, readGitHubRepository, readReleaseDownloads } from "./oss-sources.mjs";

function isAuthorized(request, secret) {
  if (!secret) return false;
  const expected = Buffer.from(`Bearer ${secret}`);
  const actual = Buffer.from(request.headers.get("authorization") ?? "");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

export async function snapshotOss(request, env = process.env, fetcher = fetch, now = new Date()) {
  if (request.method !== "GET") return new Response(null, { status: 405, headers: { Allow: "GET" } });
  if (!isAuthorized(request, env.CRON_SECRET)) return new Response(null, { status: 401 });

  const { LOOM_TELEMETRY_CLICKHOUSE_URL, LOOM_TELEMETRY_CLICKHOUSE_USER, LOOM_TELEMETRY_CLICKHOUSE_PASSWORD } = env;
  const warehouse = URL.parse(LOOM_TELEMETRY_CLICKHOUSE_URL ?? "");
  if (
    warehouse?.protocol !== "https:" ||
    warehouse.username ||
    warehouse.password ||
    !LOOM_TELEMETRY_CLICKHOUSE_USER ||
    !LOOM_TELEMETRY_CLICKHOUSE_PASSWORD
  ) {
    return new Response(null, { status: 503 });
  }
  warehouse.search = "";
  warehouse.searchParams.set("query", "INSERT INTO loom_product_analytics.oss_metrics FORMAT JSONEachRow");
  warehouse.searchParams.set("async_insert", "0");
  warehouse.searchParams.set("wait_end_of_query", "1");

  try {
    const sources = { fetcher, signal: AbortSignal.timeout(4_000) };
    const [repository, releaseDownloads, containerPulls] = await Promise.all([
      readGitHubRepository(sources),
      readReleaseDownloads(sources),
      readContainerPulls(sources),
    ]);
    const day = now.toISOString().slice(0, 10);
    const rows = [
      { source: "github", artifact: "ClickHouse/loom", metric: "stars", value: repository?.stargazers_count },
      { source: "github", artifact: "ClickHouse/loom", metric: "forks", value: repository?.forks_count },
      {
        source: "github",
        artifact: "ClickHouse/loom",
        metric: "release_asset_downloads_total",
        value: releaseDownloads,
      },
      { source: "ghcr", artifact: "ghcr.io/clickhouse/loom", metric: "container_pulls_total", value: containerPulls },
    ]
      .filter(({ value }) => Number.isSafeInteger(value) && value >= 0)
      .map((row) => JSON.stringify({ day, ...row }));
    if (!rows.length) return new Response(null, { status: 502 });

    const auth = Buffer.from(`${LOOM_TELEMETRY_CLICKHOUSE_USER}:${LOOM_TELEMETRY_CLICKHOUSE_PASSWORD}`).toString(
      "base64",
    );
    const write = await fetcher(warehouse, {
      method: "POST",
      headers: { Authorization: `Basic ${auth}`, "Content-Type": "application/x-ndjson" },
      body: rows.join("\n"),
      redirect: "error",
      signal: AbortSignal.timeout(4000),
    });
    return new Response(null, {
      status: write.ok && !write.headers.has("X-ClickHouse-Exception-Code") && !(await write.text()).trim() ? 204 : 502,
    });
  } catch {
    return new Response(null, { status: 502 });
  }
}

export default {
  fetch(request) {
    return snapshotOss(request);
  },
};
