import assert from "node:assert/strict";
import { test } from "node:test";

import { createAdoptionReader, loadPublishedAdoption } from "./adoption-data.mjs";
import { summarizeOss } from "./adoption-view.mjs";

test("calls the saved aggregate endpoint and reports monthly coverage and missing data", async (t) => {
  const queried = [];
  const registries = [];
  t.mock.method(globalThis, "fetch", async (input, init) => {
    const url = new URL(input);
    if (url.hostname === "queries.clickhouse.cloud") {
      queried.push(url.href);
      assert.equal(init.redirect, "error");
      assert.equal(init.headers.Authorization, `Basic ${Buffer.from("fixture-id:fixture-secret").toString("base64")}`);
      assert.equal(init.body, undefined);
      return Response.json({
        data: [
          {
            totals: [],
            versions: [],
            retention: [],
            activity: [],
            lifecycle: [],
            domains: ["company.example", "other.example", { domain: "private.example", accounts: 7 }],
            oss: [
              { day: "2025-01-01", source: "pypi", metric: "sdk_downloads", value: 2 },
              { day: "2026-10-01", source: "pypi", metric: "sdk_downloads", value: 3 },
              { day: "2026-11-03", source: "pypi", metric: "sdk_downloads", value: 4 },
              { day: "2026-09-03", source: "crates.io", metric: "sdk_downloads", value: 20 },
              { day: "2026-11-03", source: "crates.io", metric: "sdk_downloads", value: 100 },
              { day: "2026-11-08", source: "github", metric: "stars", value: 60 },
              { day: "2026-11-10", source: "github", metric: "stars", value: 64 },
              { day: "2026-11-07", source: "github", metric: "stars", value: 51 },
            ].map((row) => JSON.stringify(row)),
          },
        ],
      });
    }
    registries.push(url.hostname);
    assert.equal(init.headers?.Authorization, undefined);
    if (url.hostname === "crates.io") {
      assert.equal(url.pathname, "/api/v1/crates/clickhouse-loom/downloads");
      assert.match(init.headers["User-Agent"], /ClickHouse Loom/);
      return Response.json({
        version_downloads: [
          { version: 1, date: "2026-11-03", downloads: 6 },
          { version: 2, date: "2026-11-03", downloads: 2 },
          { version: 2, date: "2026-11-10", downloads: 5 },
        ],
        meta: {
          extra_downloads: [
            { date: "2026-11-03", downloads: 3 },
            { date: "2026-11-09", downloads: 0 },
          ],
        },
      });
    }
    if (url.hostname === "github.com") {
      return new Response('<span class="label">Total downloads</span><h3 title="1869">1.87K</h3>');
    }
    if (url.pathname.endsWith("/loom")) return Response.json({ stargazers_count: 77, forks_count: 20 });
    if (url.hostname === "pypistats.org") {
      return Response.json({
        data: [
          { date: "2026-11-03", category: "without_mirrors", downloads: 6 },
          { date: "2026-11-09", category: "without_mirrors", downloads: 2 },
          { date: "2026-11-09", category: "with_mirrors", downloads: 700 },
          { date: "2026-11-10", category: "without_mirrors", downloads: 5 },
        ],
      });
    }
    return new Response(null, { status: 502 });
  });
  const read = createAdoptionReader(
    {
      LOOM_ADOPTION_QUERY_ENDPOINT_URL:
        "https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001?format=CSV",
      LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
      LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
    },
    new Date("2026-11-10T08:00:00Z"),
  );
  const selfHosted = await read("self-hosted");
  assert.deepEqual(selfHosted.selfHosted.domains, ["company.example", "other.example"]);
  assert.equal(selfHosted.selfHosted.domainDirectories, null);
  assert.equal(selfHosted.selfHosted.growth, null);
  assert.equal(selfHosted.selfHosted.growthCoverage, null);
  assert.deepEqual(selfHosted.openSource, []);
  assert.deepEqual(registries, []);

  const published = read("oss");
  assert.equal(read("oss"), published);
  const result = await published;

  assert.deepEqual(queried, ["https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001?format=JSON"]);
  assert.deepEqual(result.selfHosted.domains, ["company.example", "other.example"]);
  const archived = result.openSource.find(({ id }) => id === "pypi");
  assert.equal(archived.history[0].day, "2025-01-01");
  const byLabel = Object.fromEntries(
    summarizeOss(result.openSource, 90, result.today).map((metric) => [metric.label, metric]),
  );
  const python = byLabel["Python SDK downloads"];
  assert.equal(python.value, 11);
  assert.equal(python.day, "2026-11-09");
  assert.deepEqual(python.monthly, [
    { day: "2026-10-01", value: 3, days: 1 },
    { day: "2026-11-01", value: 8, days: 2 },
  ]);
  assert.equal(byLabel["TypeScript SDK downloads"].value, null);
  assert.deepEqual(byLabel["Rust SDK downloads"].history, [
    { day: "2026-09-03", value: 20 },
    { day: "2026-11-03", value: 11 },
    { day: "2026-11-09", value: 0 },
  ]);
  assert.equal(byLabel["Rust SDK downloads"].value, 31);
  assert.equal(byLabel["Container pulls"].value, 1869);
  assert.deepEqual(byLabel["GitHub stars"].history, [
    { day: "2026-11-07", value: 51 },
    { day: "2026-11-08", value: 60 },
    { day: "2026-11-10", value: 77 },
  ]);
});

test("reads the names-only directory and retained deployment growth from the aggregate query", async (t) => {
  const start = { grain: "day", period: "2026-10-27", period_end: "2026-10-28", first_active: 1, cumulative_active: 1 };
  const latest = {
    grain: "day",
    period: "2026-11-09",
    period_end: "2026-11-10",
    first_active: 0,
    cumulative_active: 1,
  };
  const activity = { grain: "week", period: "2026-11-02", mcp_requests: 27, median_active_orgs: 2 };
  const response = {
    totals: [],
    versions: [],
    retention: [],
    activity: [JSON.stringify(activity)],
    lifecycle: [],
    oss: [],
    growth: [latest, { ...start, installation_id: "private-installation" }].map((row) => JSON.stringify(row)),
    domains: ["company.example", "other.example"],
    domain_directories: [
      { range_days: 30, domains: ["other.example"] },
      { range_days: 90, domains: ["other.example", "company.example"] },
    ].map((row) => JSON.stringify(row)),
  };
  t.mock.method(globalThis, "fetch", async () => Response.json({ data: [response] }));
  const env = {
    LOOM_ADOPTION_QUERY_ENDPOINT_URL: "https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001",
    LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
    LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
  };
  const read = () => createAdoptionReader(env, new Date("2026-11-10T08:00:00Z"))("self-hosted");
  const { selfHosted } = await read();
  assert.deepEqual(selfHosted.domainDirectories, { 30: ["other.example"], 90: ["other.example", "company.example"] });
  assert.deepEqual(selfHosted.growth, [start, latest]);
  assert.deepEqual(selfHosted.growthCoverage, { first_day: start.period, last_day: latest.period });
  assert.deepEqual(selfHosted.activity, [activity]);

  response.growth = ["invalid"];
  response.domain_directories = [
    JSON.stringify({ range_days: 30, domains: [{ domain: "private.example", requests: 123 }] }),
  ];
  const fallback = (await read()).selfHosted;
  assert.deepEqual(fallback.domains, ["company.example", "other.example"]);
  assert.deepEqual(fallback.activity, [activity]);
  assert.equal(fallback.domainDirectories, null);
  assert.equal(fallback.growth, null);
  assert.equal(fallback.growthCoverage, null);
});

test("keeps Query API credentials off unrecognized endpoint URLs", async (t) => {
  const requests = [];
  t.mock.method(console, "warn", () => {});
  t.mock.method(globalThis, "fetch", async (input, init) => {
    requests.push([new URL(input).hostname, init.headers?.Authorization]);
    return new Response(null, { status: 502 });
  });
  const read = createAdoptionReader(
    {
      LOOM_ADOPTION_QUERY_ENDPOINT_URL: "https://external.example/run/00000000-0000-0000-0000-000000000001",
      LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
      LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
    },
    new Date("2026-11-10T08:00:00Z"),
  );
  const result = await read("oss");
  assert.ok(requests.every(([host, authorization]) => host !== "external.example" && authorization === undefined));
  assert.equal(result.selfHosted.totals, undefined);
  assert.equal(result.selfHosted.domains, null);
});

test("refreshes warehouse data within thirty seconds and reuses registry metrics", async (t) => {
  let now = Date.UTC(2026, 10, 27, 18);
  t.mock.method(Date, "now", () => now);
  const originalEnv = process.env;
  t.after(() => {
    process.env = originalEnv;
  });
  process.env = {
    ...originalEnv,
    LOOM_ADOPTION_QUERY_ENDPOINT_URL: "https://queries.clickhouse.cloud/run/00000000-0000-0000-0000-000000000001",
    LOOM_ADOPTION_QUERY_KEY_ID: "fixture-id",
    LOOM_ADOPTION_QUERY_KEY_SECRET: "fixture-secret",
  };
  t.mock.method(console, "warn", () => {});
  const timeoutBySignal = new Map();
  t.mock.method(AbortSignal, "timeout", (delay) => {
    const { signal } = new AbortController();
    timeoutBySignal.set(signal, delay);
    return signal;
  });
  const warehouseTimeouts = [];
  const registryTimeouts = [];
  t.mock.method(globalThis, "fetch", async (input, init) => {
    if (new URL(input).hostname !== "queries.clickhouse.cloud") {
      registryTimeouts.push(timeoutBySignal.get(init.signal));
      return new Response(null, { status: 502 });
    }
    warehouseTimeouts.push(timeoutBySignal.get(init.signal));
    const attempt = warehouseTimeouts.length;
    if (attempt === 2) throw new DOMException("Warehouse timed out", "TimeoutError");
    const directory =
      attempt === 1
        ? { domain_directories: [30, 90].map((range_days) => JSON.stringify({ range_days, domains: [] })) }
        : attempt === 3
          ? { domain_directories: [JSON.stringify({ range_days: 30, domains: [] })] }
          : { domains: ["today.example"] };
    return Response.json({
      data: [
        {
          totals: [],
          versions: [],
          retention: [],
          activity: [],
          lifecycle: [],
          oss: [],
          ...directory,
        },
      ],
    });
  });

  const first = loadPublishedAdoption("oss");
  assert.equal((await first).warehouseAvailable, true);
  assert.deepEqual((await first).selfHosted.domainDirectories, { 30: [], 90: [] });
  now += 29_999;
  assert.equal(loadPublishedAdoption("oss"), first);
  now += 1;
  const timedOut = loadPublishedAdoption("self-hosted");
  assert.equal(loadPublishedAdoption("self-hosted"), timedOut);
  assert.equal((await timedOut).selfHosted.domains, null);
  assert.equal((await timedOut).warehouseAvailable, false);
  now += 14_999;
  assert.equal(loadPublishedAdoption("self-hosted"), timedOut);
  now += 1;
  const partial = loadPublishedAdoption("self-hosted");
  assert.equal((await partial).warehouseAvailable, false);
  now += 14_999;
  assert.equal(loadPublishedAdoption("self-hosted"), partial);
  now += 1;
  const older = await loadPublishedAdoption("oss");
  assert.equal(older.warehouseAvailable, true);
  assert.deepEqual(older.selfHosted.domains, ["today.example"]);
  assert.deepEqual(warehouseTimeouts, [30_000, 30_000, 30_000, 30_000]);
  assert.deepEqual(registryTimeouts, Array(7).fill(5_000));
});
