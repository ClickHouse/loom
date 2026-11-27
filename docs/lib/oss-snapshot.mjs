import { timingSafeEqual } from "node:crypto";

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
    const response = await fetcher("https://api.github.com/repos/ClickHouse/loom", {
      redirect: "error",
      signal: AbortSignal.timeout(4000),
    });
    if (!response.ok) return new Response(null, { status: 502 });
    const { stargazers_count, forks_count } = await response.json();
    const values = { stars: stargazers_count, forks: forks_count };
    if (!Object.values(values).every((value) => Number.isSafeInteger(value) && value >= 0)) {
      return new Response(null, { status: 502 });
    }
    const day = now.toISOString().slice(0, 10);
    const rows = Object.entries(values).map(([metric, value]) =>
      JSON.stringify({ day, source: "github", artifact: "ClickHouse/loom", metric, value }),
    );
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
