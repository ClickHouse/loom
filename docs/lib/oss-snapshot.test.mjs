import assert from "node:assert/strict";
import { test } from "node:test";

import { snapshotOss } from "./oss-snapshot.mjs";

const endpoint = "https://telemetry.example.test/api/oss-snapshot";
const env = {
  CRON_SECRET: "job-secret",
  LOOM_TELEMETRY_CLICKHOUSE_URL: "https://analytics.example.test:8443",
  LOOM_TELEMETRY_CLICKHOUSE_USER: "writer",
  LOOM_TELEMETRY_CLICKHOUSE_PASSWORD: "write-password",
};

test("allows the scheduled job to record one snapshot per counter and UTC date", async () => {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url: new URL(url), init });
    return calls.length === 1 ? Response.json({ stargazers_count: 42, forks_count: 7 }) : new Response("");
  };
  const request = new Request(endpoint, { headers: { Authorization: "Bearer job-secret" } });
  const response = await snapshotOss(request, env, fetcher, new Date("2026-11-24T02:30:00Z"));
  assert.equal(response.status, 204);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url.hostname, "api.github.com");
  assert.equal(calls[1].url.hostname, "analytics.example.test");
  assert.match(calls[1].url.searchParams.get("query"), /INSERT INTO loom_product_analytics\.oss_metrics/);
  const rows = calls[1].init.body.split("\n").map((row) => JSON.parse(row));
  assert.deepEqual(rows, [
    { day: "2026-11-24", source: "github", artifact: "ClickHouse/loom", metric: "stars", value: 42 },
    { day: "2026-11-24", source: "github", artifact: "ClickHouse/loom", metric: "forks", value: 7 },
  ]);
});

test("rejects unauthenticated calls before making requests", async () => {
  const fetcher = () => assert.fail("unauthorized request reached the network");
  assert.equal((await snapshotOss(new Request(endpoint), env, fetcher)).status, 401);
  assert.equal((await snapshotOss(new Request(endpoint), {}, fetcher)).status, 401);
});
