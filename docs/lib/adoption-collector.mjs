import { createHash } from "node:crypto";

const maxBodyBytes = 1_048_576;
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
const dailyActivityFields = ["daily_successful_requests", "active_accounts", "serving_processes", "domain_activity"];
const reportFields = new Set([
  "schema_version",
  "installation_id",
  "day",
  "window_start",
  "window_end",
  "loom_version",
  "clickhouse_version",
  "user_domains",
  ...dailyActivityFields,
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

function hasValidDailyActivity(report, domains) {
  const { active_accounts: accounts, serving_processes: processes, domain_activity: rows = [] } = report;
  if (report.schema_version < 3) {
    return dailyActivityFields.every((name) => !Object.hasOwn(report, name));
  }
  if (!Array.isArray(rows) || rows.length > domains.size) return false;
  if (accounts === undefined || accounts === null)
    return (processes === undefined || processes === null) && rows.length === 0;
  if (!isCount(accounts) || accounts > report.daily_successful_requests) return false;
  if (
    processes !== undefined &&
    processes !== null &&
    (!isCount(processes) || processes > report.daily_successful_requests)
  )
    return false;
  return (
    rows.every(
      (row) =>
        isObject(row) &&
        Object.keys(row).length === 4 &&
        row.successful_requests === domains.get(row.domain) &&
        isCount(row.active_accounts) &&
        row.active_accounts <= accounts &&
        isCount(row.successful_requests) &&
        row.active_accounts <= row.successful_requests &&
        (row.serving_processes === null ||
          (isCount(row.serving_processes) &&
            row.serving_processes <= (processes ?? 0) &&
            row.serving_processes <= row.successful_requests)),
    ) && new Set(rows.map(({ domain }) => domain)).size === rows.length
  );
}

function isReport(report, now) {
  if (!isObject(report) || Object.keys(report).some((name) => !reportFields.has(name))) {
    return false;
  }
  if (
    ![1, 2, 3].includes(report.schema_version) ||
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

  const daily = report.schema_version === 3;
  const successfulRequests = daily ? report.daily_successful_requests : report.successful_requests;
  if (
    !isCount(successfulRequests) ||
    (daily &&
      (new Date(end).getUTCHours() !== 0 ||
        successfulRequests < report.successful_requests ||
        report.successful_requests > report.requests - report.server_errors))
  )
    return false;

  const domains = report.user_domains;
  if (!Array.isArray(domains) || (!daily && domains.length > 30)) return false;
  const validDomains = domains.every(
    (row) =>
      isObject(row) &&
      Object.keys(row).length === 3 &&
      matches(domainPattern, row.domain) &&
      row.domain.length <= 253 &&
      isCount(row.user_count) &&
      row.user_count > 0 &&
      isCount(row.successful_requests) &&
      (!daily || row.successful_requests > 0),
  );
  if (!validDomains) return false;
  const domainCounts = new Map(domains.map(({ domain, successful_requests }) => [domain, successful_requests]));
  return (
    domainCounts.size === domains.length &&
    domains.reduce((count, row) => count + row.successful_requests, 0) <= successfulRequests &&
    hasValidDailyActivity(report, domainCounts)
  );
}

async function readBody(request) {
  const reader = request.body?.getReader();
  if (!reader) return null;
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return Buffer.concat(chunks, size);
    if (size + value.length > maxBodyBytes) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
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

export default {
  fetch(request) {
    return handleAdoption(request);
  },
};
