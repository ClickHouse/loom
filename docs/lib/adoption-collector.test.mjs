import assert from "node:assert/strict";
import { test } from "node:test";
import { handleAdoption } from "./adoption-collector.mjs";

const report = {
  schema_version: 1,
  installation_id: "52ab66e6-a009-5800-b564-f03ce9e7efe3",
  day: "2026-06-01",
  window_start: "2026-06-01T00:00:00+00:00",
  window_end: "2026-06-01T12:00:00+00:00",
  loom_version: "1.3.0",
  clickhouse_version: "26.10.4.207",
  total_namespaces: 2,
  requests: 3,
  successful_requests: 3,
  server_errors: 0,
  active_orgs: 1,
  active_namespaces: 2,
  active_agents: 2,
  active_sessions: 2,
  memory_requests: 2,
  code_requests: 1,
  knowledge_requests: 0,
  conversation_requests: 0,
  tracker_requests: 0,
  task_requests: 0,
  mcp_requests: 3,
  http_requests: 0,
  llm_calls: 1,
  input_tokens: 4,
  output_tokens: 2,
  embedding_tokens: 0,
  user_domains: [{ domain: "example.xn--p1ai", user_count: 2, successful_requests: 1 }],
};

const env = {
  LOOM_TELEMETRY_CLICKHOUSE_URL: "https://warehouse.example/",
  LOOM_TELEMETRY_CLICKHOUSE_USER: "test-writer",
  LOOM_TELEMETRY_CLICKHOUSE_PASSWORD: "example",
};
const now = Date.parse("2026-06-01T14:00:00Z");
const request = (body) =>
  new Request("https://telemetry.example/api/v1/adoption", {
    method: "POST",
    body,
    headers: { "Content-Type": "application/json" },
  });

test("acknowledges a report after storage and sends the same deduplication token on retry", async () => {
  const tokens = [];
  const fetcher = async (url, options) => {
    assert.equal(url.searchParams.get("async_insert"), "0");
    assert.equal(url.searchParams.get("wait_end_of_query"), "1");
    assert.equal(options.redirect, "error");
    assert.deepEqual(JSON.parse(options.body), report);
    tokens.push(url.searchParams.get("insert_deduplication_token"));
    return new Response();
  };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const result = await handleAdoption(request(JSON.stringify(report)), { env, fetcher, now });
    assert.equal(result.status, 200);
    assert.deepEqual(await result.json(), {
      stored: true,
      installation_id: report.installation_id,
      window_start: report.window_start,
    });
  }
  assert.match(tokens[0], /^[0-9a-f]{64}$/);
  assert.equal(tokens[0], tokens[1]);
});

test("rejects extra fields, malformed identities and windows, and oversized requests before storage", async () => {
  const settings = { env, now, fetcher: () => assert.fail("invalid report reached storage") };
  const invalid = [
    { ...report, prompt: "private text" },
    { ...report, schema_version: 4 },
    { ...report, installation_id: "host.example" },
    { ...report, user_domains: [{ domain: "person@example.test", user_count: 1, successful_requests: 1 }] },
    { ...report, window_end: "2026-06-02T12:00:00+00:00" },
    { ...report, day: "2026-06-02" },
  ];
  for (const body of invalid) {
    assert.equal((await handleAdoption(request(JSON.stringify(body)), settings)).status, 400);
  }
  assert.equal((await handleAdoption(request(" ".repeat(1_048_577)), settings)).status, 413);
});

test("preserves the public domain report version in storage", async () => {
  const publicReport = { ...report, schema_version: 2 };
  const fetcher = (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), publicReport);
    return Promise.resolve(new Response());
  };
  const response = await handleAdoption(request(JSON.stringify(publicReport)), { env, fetcher, now });
  assert.equal(response.status, 200);
});

const dailyReport = {
  ...report,
  schema_version: 3,
  window_start: "2026-06-01T12:00:00+00:00",
  window_end: "2026-06-02T00:00:00+00:00",
  daily_successful_requests: 15,
  active_accounts: 4,
  serving_processes: 3,
  user_domains: [{ ...report.user_domains[0], successful_requests: 11 }],
  domain_activity: [
    { domain: report.user_domains[0].domain, active_accounts: 3, successful_requests: 11, serving_processes: 2 },
  ],
};
const dailyReportNow = Date.parse("2026-06-02T01:00:00Z");

test("stores daily activity, including a measured zero and missing process counts", async () => {
  const packets = [
    dailyReport,
    {
      ...dailyReport,
      daily_successful_requests: 0,
      successful_requests: 0,
      active_accounts: 0,
      serving_processes: 0,
      user_domains: [],
      domain_activity: [],
    },
    {
      ...dailyReport,
      serving_processes: null,
      domain_activity: [{ ...dailyReport.domain_activity[0], serving_processes: null }],
    },
  ];
  for (const packet of packets) {
    const fetcher = (_url, options) => {
      assert.deepEqual(JSON.parse(options.body), packet);
      return Promise.resolve(new Response());
    };
    const response = await handleAdoption(request(JSON.stringify(packet)), { env, fetcher, now: dailyReportNow });
    assert.equal(response.status, 200);
  }
});

test("accepts daily activity only for a complete UTC day and shared domain names", async () => {
  const settings = { env, now: dailyReportNow, fetcher: () => assert.fail("invalid report reached storage") };
  const [domain] = dailyReport.domain_activity;
  const invalid = [
    { ...dailyReport, schema_version: 2 },
    { ...report, schema_version: 3, daily_successful_requests: 3, active_accounts: 1, domain_activity: [] },
    { ...dailyReport, daily_successful_requests: 3 },
    { ...dailyReport, active_accounts: null },
    { ...dailyReport, active_accounts: -1 },
    { ...dailyReport, serving_processes: -1 },
    { ...dailyReport, domain_activity: [domain, domain] },
    { ...dailyReport, domain_activity: [{ ...domain, active_accounts: 5 }] },
    { ...dailyReport, domain_activity: [{ ...domain, successful_requests: 2 }] },
    { ...dailyReport, domain_activity: [{ ...domain, serving_processes: 4 }] },
    { ...dailyReport, serving_processes: null, domain_activity: [domain] },
    { ...dailyReport, domain_activity: [{ ...domain, domain: "another.example" }] },
    { ...dailyReport, domain_activity: [{ ...domain, subject: "private" }] },
  ];
  for (const packet of invalid) {
    assert.equal((await handleAdoption(request(JSON.stringify(packet)), settings)).status, 400);
  }
});

test("accepts all active daily domain names, including domains omitted by the legacy cap", async () => {
  const domains = ["gmail.com", ...Array.from({ length: 34 }, (_, index) => `contact-${index}.example`)];
  const packet = {
    ...dailyReport,
    successful_requests: 0,
    daily_successful_requests: domains.length,
    user_domains: domains.map((domain) => ({ domain, user_count: 1, successful_requests: 1 })),
    domain_activity: domains.map((domain) => ({
      domain,
      active_accounts: 1,
      successful_requests: 1,
      serving_processes: 1,
    })),
  };
  const fetcher = (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), packet);
    return Promise.resolve(new Response());
  };
  const options = { now: dailyReportNow, env, fetcher };
  assert.equal((await handleAdoption(request(JSON.stringify(packet)), options)).status, 200);
  const legacy = {
    ...report,
    schema_version: 2,
    user_domains: packet.user_domains,
    successful_requests: domains.length,
  };
  assert.equal((await handleAdoption(request(JSON.stringify(legacy)), options)).status, 400);
});

test("rejects inconsistent request totals in daily reports", async () => {
  const invalid = [
    {
      ...dailyReport,
      daily_successful_requests: 0,
      active_accounts: 0,
      serving_processes: 0,
      user_domains: [],
      domain_activity: [],
    },
    { ...dailyReport, server_errors: 1 },
  ];
  const settings = { env, now: dailyReportNow, fetcher: () => assert.fail("invalid report reached storage") };
  for (const packet of invalid) {
    assert.equal((await handleAdoption(request(JSON.stringify(packet)), settings)).status, 400);
  }
});

test("returns an error while writes fail or the warehouse is unconfigured", async () => {
  const fetcher = () => Promise.resolve(new Response("Insert error", { status: 200 }));
  const failure = await handleAdoption(request(JSON.stringify(report)), { env, fetcher, now });
  assert.equal(failure.status, 503);
  assert.deepEqual(await failure.json(), { error: "store_unavailable" });
  const unconfigured = await handleAdoption(request(JSON.stringify(report)), { env: {}, now });
  assert.equal(unconfigured.status, 503);
});

test("accepts a live domain snapshot before the UTC day ends", async () => {
  const observed_at = "2026-11-28T07:20:00+00:00";
  const report = {
    schema_version: 4,
    installation_id: "31abbc20-dcf3-558b-b41d-4ca3265db296",
    day: "2026-11-28",
    observed_at,
    loom_version: "1.3.0",
    clickhouse_version: "26.10.2.551",
    domain_activity: [{ domain: "client.example", active_accounts: 6, successful_requests: 25, serving_processes: 2 }],
  };
  const writes = [];
  const request = new Request("https://example.test/api/v1/adoption", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(report),
  });
  const response = await handleAdoption(request, {
    now: Date.parse(observed_at) + 60_000,
    env: {
      LOOM_TELEMETRY_CLICKHOUSE_URL: "https://warehouse.example",
      LOOM_TELEMETRY_CLICKHOUSE_USER: "test",
      LOOM_TELEMETRY_CLICKHOUSE_PASSWORD: "test",
    },
    fetcher: async (url, options) => {
      writes.push([url.searchParams.get("query"), JSON.parse(options.body)]);
      return new Response("");
    },
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { stored: true, installation_id: report.installation_id, observed_at });
  assert.deepEqual(writes, [
    ["INSERT INTO loom_product_analytics.adoption_domain_snapshots FORMAT JSONEachRow", report],
  ]);
});
