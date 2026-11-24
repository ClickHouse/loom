import { createHash } from "node:crypto";

const maxBodyBytes = 16_384;
const halfDayMs = 12 * 60 * 60 * 1_000;
const maxAgeMs = 14 * halfDayMs;
const installationPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const timestampPattern = /^\d{4}-\d{2}-\d{2}T(?:00|12):00:00\+00:00$/;
const versionPattern = /^[0-9][0-9A-Za-z.+-]{0,63}$/;
const domainPattern =
  /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?)$/;
const countFields = [
  "total_namespaces",
  "requests",
  "successful_requests",
  "server_errors",
  "active_orgs",
  "active_namespaces",
  "active_agents",
  "active_sessions",
  "memory_requests",
  "code_requests",
  "knowledge_requests",
  "conversation_requests",
  "tracker_requests",
  "task_requests",
  "mcp_requests",
  "http_requests",
  "llm_calls",
  "input_tokens",
  "output_tokens",
  "embedding_tokens",
];
const reportFields = new Set([
  "schema_version",
  "installation_id",
  "day",
  "window_start",
  "window_end",
  "loom_version",
  "clickhouse_version",
  "user_domains",
  ...countFields,
]);

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isCount = (value) => Number.isSafeInteger(value) && value >= 0;
const matches = (pattern, value) => typeof value === "string" && pattern.test(value);

function windowTime(value) {
  if (!matches(timestampPattern, value)) return NaN;
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return NaN;
  const canonical = new Date(timestamp).toISOString().replace(".000Z", "+00:00");
  return canonical === value ? timestamp : NaN;
}

function isReport(report, now) {
  if (!isObject(report) || Object.keys(report).some((name) => !reportFields.has(name))) {
    return false;
  }
  if (
    report.schema_version !== 1 ||
    !matches(installationPattern, report.installation_id) ||
    !matches(versionPattern, report.loom_version) ||
    !matches(versionPattern, report.clickhouse_version) ||
    !countFields.every((name) => isCount(report[name]))
  )
    return false;
  const start = windowTime(report.window_start);
  const end = windowTime(report.window_end);
  if (end - start !== halfDayMs || end > now || end < now - maxAgeMs || report.day !== report.window_start.slice(0, 10))
    return false;

  const domains = report.user_domains;
  if (!Array.isArray(domains) || domains.length > 30) return false;
  const validDomains = domains.every(
    (row) =>
      isObject(row) &&
      Object.keys(row).length === 3 &&
      matches(domainPattern, row.domain) &&
      row.domain.length <= 253 &&
      isCount(row.user_count) &&
      row.user_count > 0 &&
      isCount(row.successful_requests),
  );
  return (
    validDomains &&
    new Set(domains.map(({ domain }) => domain)).size === domains.length &&
    domains.reduce((count, row) => count + row.successful_requests, 0) <= report.successful_requests
  );
}

async function readBody(request) {
  const reader = request.body?.getReader();
  if (!reader) return null;
  const buffer = new Uint8Array(maxBodyBytes);
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return buffer.subarray(0, size);
    if (size + value.length > maxBodyBytes) {
      await reader.cancel();
      return null;
    }
    buffer.set(value, size);
    size += value.length;
  }
}

async function storeReport(report, env, fetcher) {
  const {
    LOOM_TELEMETRY_CLICKHOUSE_URL: address,
    LOOM_TELEMETRY_CLICKHOUSE_USER: user,
    LOOM_TELEMETRY_CLICKHOUSE_PASSWORD: password,
  } = env;
  if (!address || !user || !password) return false;
  const url = new URL(address);
  if (url.protocol !== "https:" || url.username || url.password) return false;
  url.searchParams.set("query", "INSERT INTO loom_product_analytics.adoption_reports FORMAT JSONEachRow");
  url.searchParams.set("date_time_input_format", "best_effort");
  url.searchParams.set("async_insert", "0");
  url.searchParams.set("wait_end_of_query", "1");
  url.searchParams.set(
    "insert_deduplication_token",
    createHash("sha256").update(`${report.installation_id}:${report.window_start}`).digest("hex"),
  );
  const response = await fetcher(url, {
    method: "POST",
    headers: {
      Authorization: `Basic ${Buffer.from(`${user}:${password}`).toString("base64")}`,
      "Content-Type": "application/x-ndjson",
    },
    body: `${JSON.stringify(report)}\n`,
    redirect: "error",
    signal: AbortSignal.timeout(6_000),
  });
  return response.ok && !response.headers.has("X-ClickHouse-Exception-Code") && !(await response.text()).trim();
}

function reply(body, status, headers) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", ...headers } });
}

export async function handleAdoption(request, { env = process.env, fetcher = fetch, now = Date.now() } = {}) {
  if (request.method !== "POST") return reply({ error: "method_not_allowed" }, 405, { Allow: "POST" });
  if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
    return reply({ error: "unsupported_media_type" }, 415);
  }
  if (Number(request.headers.get("content-length")) > maxBodyBytes) {
    return reply({ error: "report_too_large" }, 413);
  }
  let report;
  try {
    const body = await readBody(request);
    if (body === null) return reply({ error: "report_too_large" }, 413);
    report = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
  } catch {
    return reply({ error: "invalid_json" }, 400);
  }
  if (!isReport(report, now)) return reply({ error: "invalid_report" }, 400);

  try {
    if (await storeReport(report, env, fetcher)) {
      const { installation_id, window_start } = report;
      return reply({ stored: true, installation_id, window_start }, 200);
    }
  } catch {
    // Store errors can contain credentials or report data.
  }
  return reply({ error: "store_unavailable" }, 503);
}
